"""bilibili（哔哩哔哩）专用站点适配器。

链路：视频页/列表页 -> 提取视频卡片（BV 链接）
     -> view API 取 aid/cid（脚本请求拿不到页面播放数据）
     -> x/player/playurl -> durl 单文件 mp4（含音视频，未登录最高 360p）

与 generic 的关系：本适配器 match 命中 bilibili 域名时优先使用。
generic 的 yt-dlp 虽能识别 bilibili，但返回 dash 分离流（音视频分开），
Android 端无法合并（无声）；本适配器直接取单文件 mp4/flv。
"""
import json
import logging
import re
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from parsel import Selector

from app.config import DEFAULT_HEADERS
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

log = logging.getLogger(__name__)

HOST = "bilibili.com"
UA = DEFAULT_HEADERS.get("User-Agent", "Mozilla/5.0")
REFERER = "https://www.bilibili.com/"

_QUALITY_MAP = {16: "360p", 32: "480p", 64: "720p", 80: "1080p", 112: "1080p+", 116: "1080p60"}
_BV_RE = re.compile(r"/video/(BV[0-9A-Za-z]+)")


class SiteBilibili:
    name = "bilibili"

    def match(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host == HOST or host.endswith("." + HOST)

    # ---------------- 列表/搜索/分区页卡片 ----------------
    def parse_cards(self, html: str, base_url: str) -> list:
        # 搜索页：B 站搜索结果是 JS 客户端渲染，HTML 只有第 1 页 SSR 卡片、
        # 第 2 页起完全没卡片；直接用官方搜索 API 按页取（每页 20 条）。
        if "search.bilibili.com" in urlparse(base_url).netloc.lower():
            try:
                cards = self._search_cards(base_url)
                if cards:
                    return cards
            except Exception as e:  # noqa: BLE001
                log.info("bilibili 搜索 API 失败，退回 HTML 解析: %s", e)
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

    @staticmethod
    def _search_cards(base_url: str) -> list:
        """官方搜索 API：/x/web-interface/search/all/v2（无需 wbi 签名）。"""
        q = parse_qs(urlparse(base_url).query)
        keyword = (q.get("keyword") or [""])[0]
        page = 1
        for key in ("page", "p", "pn"):
            if q.get(key) and q[key][0].isdigit():
                page = int(q[key][0])
                break
        if not keyword:
            return []
        api = ("https://api.bilibili.com/x/web-interface/search/all/v2"
               "?keyword=%s&page=%d" % (quote(keyword), page))
        data = json.loads(fetch_text(api, headers={"Referer": REFERER}))
        cards = []
        for block in ((data.get("data") or {}).get("result") or []):
            if block.get("result_type") != "video":
                continue
            for v in block.get("data") or []:
                bvid = (v.get("bvid") or "").strip()
                if not bvid:
                    continue
                title = re.sub(r"<[^>]+>", "", v.get("title") or "").strip()
                cards.append(VideoCard(
                    title=title,
                    url="https://www.bilibili.com/video/" + bvid,
                    kind="video",
                    poster=(v.get("pic") or "").strip() or "",
                    duration=SiteBilibili._fmt_duration(v.get("duration") or ""),
                    source_page=base_url,
                ))
        return cards

    @staticmethod
    def _fmt_duration(raw: str) -> str:
        """搜索 API 的 duration 形如 89:0（分钟:秒），转成 H:MM:SS / MM:SS。"""
        if not raw or ":" not in raw:
            return raw
        parts = raw.split(":")
        try:
            secs = int(parts[-1]) if len(parts) == 2 else 0
            mins = int(parts[-2]) if len(parts) >= 2 else int(parts[0])
        except (TypeError, ValueError):
            return raw
        total = mins * 60 + secs
        h, rem = divmod(total, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    # ---------------- 单页/视频页快速解析 ----------------
    def single_source(self, url: str):
        try:
            return self.resolve_video(VideoCard(title="", url=url, kind="video"))
        except Exception as e:
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

        # B 站对脚本请求（urllib）返回无播放数据的降级页，不解析页面；
        # 用 view API 拿 aid/cid/标题（无需登录）
        view = json.loads(fetch_text(
            "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid,
            headers={"Referer": REFERER},
        ))
        vd = (view.get("data") or {})
        cid = vd.get("cid")
        if not cid:
            return None
        title = title or vd.get("title") or ""

        # B 站 wbi 版 playurl 对脚本请求返回 v_voucher（空 data）；
        # 用老的 x/player/playurl 接口即可直接拿到单文件 mp4（匿名最高 360p）。
        params = {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 0, "fnver": 0, "fourk": 1}
        api = "https://api.bilibili.com/x/player/playurl?" + urlencode(params)
        result = json.loads(fetch_text(api, headers={"Referer": REFERER}))
        data = result.get("data") or {}
        durl = data.get("durl") or []
        if not durl:
            return None
        stream = durl[0]
        stream_url = stream.get("url") or ""
        if not stream_url:
            return None
        qn = data.get("quality") or 0
        note = "bilibili 官方单文件源"
        if len(durl) > 1:
            note += "（多段源，仅下载第一段）"
        return VideoSource(
            url=stream_url,
            title=title,
            protocol="http",
            headers={"Referer": REFERER, "User-Agent": UA},
            note=note,
            quality=_QUALITY_MAP.get(qn, f"{qn}p"),
        )
