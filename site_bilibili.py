"""bilibili（哔哩哔哩）专用站点适配器。

链路：视频页/列表页 -> 提取视频卡片（BV 链接）
     -> 视频页 __INITIAL_STATE__ 取 aid/cid -> wbi 签名 playurl API
     -> durl 单文件 mp4（含音视频，未登录最高 360p）

与 generic 的关系：本适配器 match 命中 bilibili 域名时优先使用。
generic 的 yt-dlp 虽能识别 bilibili，但返回 dash 分离流（音视频分开），
Android 端无法合并（无声）；本适配器直接取单文件 mp4/flv。
"""
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlencode, urljoin, urlparse

from parsel import Selector

from app.config import DEFAULT_HEADERS
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

log = logging.getLogger(__name__)

HOST = "bilibili.com"
UA = DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0")
REFERER = "https://www.bilibili.com/"

# wbi 签名：mixin key 打乱表（B 站公开算法）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
_QUALITY_MAP = {16: "360p", 32: "480p", 64: "720p", 80: "1080p", 112: "1080p+", 116: "1080p60"}
_BV_RE = re.compile(r"/video/(BV[0-9A-Za-z]+)")
_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S,
)

_wbi_keys = {"img": None, "sub": None, "at": 0.0}


class SiteBilibili:
    name = "bilibili"

    def match(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host == HOST or host.endswith("." + HOST)

    # ---------------- 列表/搜索/分区页卡片 ----------------
    def parse_cards(self, html: str, base_url: str) -> list:
        sel = Selector(text=html)
        cards, seen = [], set()
        for a in sel.css('a[href*="/video/BV"]'):
            href = (a.attrib.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(base_url, href)
            if url in seen:
                continue
            seen.add(url)
            img = a.css("img")
            if img:
                title = (img.attrib.get("alt") or "").strip()
                poster = img.attrib.get("src") or img.attrib.get("data-src") or ""
            else:
                title = (a.attrib.get("title") or "").strip() or \
                    " ".join(a.css("::text").getall()).strip()
                poster = ""
            dur = (a.css('[class*="duration"]::text').get() or "").strip()
            cards.append(VideoCard(
                title=title,
                url=url,
                kind="video",
                poster=poster,
                duration=dur,
                source_page=base_url,
            ))
        return cards

    # ---------------- 单页/视频页快速解析 ----------------
    def single_source(self, url: str):
        try:
            return self.resolve_video(VideoCard(title="", url=url, kind="video"))
        except Exception as e:
            print("[bili] single_source 失败:", url, repr(e), flush=True)
            log.info("bilibili single_source 失败 %s: %s", url, e)
            return None

    # ---------------- 视频页 -> 单文件 mp4 ----------------
    def resolve_video(self, card) -> VideoSource:
        url = card.url if hasattr(card, "url") else card["url"]
        title = getattr(card, "title", "") or (card.get("title", "") if isinstance(card, dict) else "")
        m = _BV_RE.search(url)
        if not m:
            return None
        bvid = m.group(1)

        print("[bili] fetch 视频页开始", flush=True)
        html = fetch_text(url, headers={"Referer": REFERER})
        print("[bili] 视频页大小:", len(html), flush=True)
        aid, cid = None, None
        im = _INITIAL_STATE_RE.search(html)
        print("[bili] INITIAL_STATE 提取:", "成功" if im else "失败", flush=True)
        if im:
            try:
                state = json.loads(im.group(1))
                vd = state.get("videoData") or {}
                aid = vd.get("aid")
                cid = vd.get("cid")
                title = title or vd.get("title") or ""
            except Exception as e:  # noqa: BLE001
                log.info("bilibili INITIAL_STATE 解析失败 %s: %s", url, e)
        print("[bili] aid/cid:", aid, cid, flush=True)
        if not cid:
            # 页面未内嵌时退化为仅标题
            tm = re.search(r"<title>(.*?)</title>", html, re.S)
            if tm:
                title = title or tm.group(1).strip()
            return None

        img_key, sub_key = self._wbi_keys()
        params = _enc_wbi(
            {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 0, "fnver": 0, "fourk": 1},
            img_key, sub_key,
        )
        api = "https://api.bilibili.com/x/player/playurl?" + urlencode(params)
        print("[bili] playurl 请求", flush=True)
        result = json.loads(fetch_text(api, headers={"Referer": REFERER}))
        print("[bili] playurl code:", result.get("code"), result.get("message"), flush=True)
        data = result.get("data") or {}
        durl = data.get("durl") or []
        if not durl:
            return None
        stream = durl[0]
        stream_url = stream.get("url") or ""
        if not stream_url:
            return None
        qn = data.get("quality") or 0
        return VideoSource(
            url=stream_url,
            title=title,
            protocol="http",
            headers={"Referer": REFERER, "User-Agent": UA},
            note="bilibili 官方单文件源",
            quality=_QUALITY_MAP.get(qn, f"{qn}p"),
        )

    @staticmethod
    def _wbi_keys():
        """获取 wbi 签名密钥（nav 接口，缓存 1 小时）。"""
        now = time.time()
        if _wbi_keys["img"] and now - _wbi_keys["at"] < 3600:
            return _wbi_keys["img"], _wbi_keys["sub"]
        nav = json.loads(fetch_text(
            "https://api.bilibili.com/x/web-interface/nav", headers={"Referer": REFERER},
        ))
        wbi = ((nav.get("data") or {}).get("wbi_img") or {})
        img_url = wbi.get("img_url") or ""
        sub_url = wbi.get("sub_url") or ""
        if not img_url or not sub_url:
            raise RuntimeError("bilibili nav 未返回 wbi 密钥")
        img_key = img_url.rsplit("/", 1)[1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
        _wbi_keys["img"], _wbi_keys["sub"], _wbi_keys["at"] = img_key, sub_key, now
        return img_key, sub_key


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def _enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """wbi 签名：wts 时间戳 + w_rid（mixin key md5）。"""
    mixin = _mixin_key(img_key + sub_key)
    params["wts"] = int(time.time())
    ordered = dict(sorted(params.items()))
    query = urlencode(ordered)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params
