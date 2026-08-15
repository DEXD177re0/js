"""91porna 站点适配器（已实测打通）。

链路：列表/搜索页 -> 视频卡片（detail 或 avdetail 都是视频）
     -> 详情页 og:video -> embed 播放页
     -> 混淆 JS 拼出 embed_play.js -> packer 解码 -> 真实 m3u8。
"""
import re
import time
from urllib.parse import urljoin, urlparse

from parsel import Selector

from app.config import DEFAULT_HEADERS
from app.core.decode import decode_packed
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

HOST = "91porna.com"
_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360|240)p", re.I)


class Site91porna:
    name = "91porna"

    def match(self, url: str) -> bool:
        return urlparse(url).netloc.split(":")[0].lower().endswith(HOST)

    # ---------------- 列表页 ----------------
    def parse_cards(self, html: str, base_url: str) -> list:
        sel = Selector(text=html)
        cards = []
        for item in sel.css(".video-item"):
            href = item.css("a[href*='video_key']::attr(href)").get()
            if not href:
                continue
            img = item.css("img")
            title = (img.attrib.get("alt") or "").strip()
            poster = img.attrib.get("data-src") or img.attrib.get("src") or ""
            dur = (item.css(".bg-black::text").get("") or "").strip()
            cards.append(
                VideoCard(
                    title=title,
                    url=urljoin(base_url, href),
                    kind="video",  # detail/avdetail 两种卡片都是视频（站点均标 video 类型）
                    poster=poster,
                    duration=dur,
                    source_page=base_url,
                )
            )
        return cards

    # ---------------- 详情页 -> m3u8 ----------------
    def resolve_video(self, card) -> VideoSource:
        card_url = card.url if hasattr(card, "url") else card["url"]
        title = getattr(card, "title", None) or card.get("title", "")
        origin = f"{urlparse(card_url).scheme}://{urlparse(card_url).netloc}/"
        html = fetch_text(card_url, headers={"Referer": origin})
        # og:video 可能先出现 text/html 类型，取第一个 http(s) 的 embed 地址
        m = re.search(r'property="og:video[^"]*"\s+content="(https?://[^"]+)"', html)
        if not m:
            m = re.search(r'href="([^"]*embed[^"]*id=\d+[^"]*)"', html)
        if not m:
            raise ValueError("详情页未找到 embed 播放页")
        embed_url = urljoin(card_url, m.group(1))
        src = self._resolve_embed(embed_url)
        src.title = title
        return src

    def _resolve_embed(self, embed_url: str) -> VideoSource:
        html = fetch_text(embed_url, headers={"Referer": embed_url})
        base, u = self._extract_play_info(html)
        t = int(time.time())
        play_url = urljoin(f"https://{HOST}/", base + u + "&t=" + str(t))
        js = fetch_text(play_url, headers={"Referer": embed_url})
        dec = decode_packed(js)
        m3 = re.search(r"https?://[^\s\"']+?\.m3u8[^\s\"']*", dec)
        if not m3:
            raise ValueError("播放器脚本未包含 m3u8 地址")
        return VideoSource(
            url=m3.group(0).rstrip("\\"),
            protocol="hls",
            headers={
                "Referer": f"https://{HOST}/",
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
            note="91porna AES-128，N_m3u8DL-CLI 自动解密",
            quality=self._quality_of(m3.group(0)),
        )

    @staticmethod
    def _quality_of(url: str) -> str:
        """从 m3u8 地址里识别清晰度数字标签；识别不到返回空（界面显示 —）。"""
        m = _QUALITY_RE.search(url)
        return f"{int(m.group(1))}p" if m else ""

    @staticmethod
    def _extract_play_info(embed_html: str):
        """从 embed 页（或其解码文本）提取 embed_play.js 地址前缀与 u 令牌。"""
        for text in (embed_html, decode_packed(embed_html)):
            m = re.search(r"((?:https?:)?//?[^\"']*embed_play\.js\?[^\"']*?&u=)", text)
            if not m:
                continue
            base = m.group(1)
            um = re.search(r'encodeURIComponent\(\s*"([0-9a-fA-F]+)"', text)
            if not um:
                um = re.search(r"&u=([0-9a-fA-F]+)", text)
            if um:
                return base, um.group(1)
        raise ValueError("embed 页未找到播放器令牌")