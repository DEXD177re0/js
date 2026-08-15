"""18mh（禁漫天堂）专用站点适配器。

链路：列表/搜索页 -> 视频卡片（a[href*="/mv/detail/"]，同一视频封面+标题两个链接按 URL 去重）
     -> 详情页 _detail_ JSON / 播放器配置里直接带 m3u8（auth_key 防盗链，N_m3u8DL-CLI 自动处理）。

与 generic 的关系：本适配器 match 命中 18mh 域名时优先使用，generic 只兜底其他网站。
"""
import logging
import re
from urllib.parse import urljoin, urlparse

from parsel import Selector

from app.config import DEFAULT_HEADERS
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

log = logging.getLogger(__name__)

HOST = "18mh.net"
BS = chr(92)  # 反斜杠
_M3U8_RE = re.compile(r"https?://[^\s\"'<>\x5c]+?\.m3u8[^\s\"'<>\x5c]*", re.I)
_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360|240)p", re.I)


class Site18mh:
    name = "18mh"

    def match(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host == HOST or host.endswith("." + HOST)

    # ---------------- 列表/搜索页卡片 ----------------
    def parse_cards(self, html: str, base_url: str) -> list:
        sel = Selector(text=html)
        cards, seen = [], set()
        for a in sel.css('a[href*="/mv/detail/"]'):
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
                poster = img.attrib.get("data-src") or img.attrib.get("src") or ""
            else:
                title = " ".join(a.css("h2 ::text, .title ::text, .name ::text").getall()).strip()
                poster = ""
            dur = (
                a.xpath('following-sibling::*//span[contains(@class, "time")]/text()').get()
                or a.css('[class*="time"]::text').get()
                or ""
            ).strip()
            cards.append(VideoCard(
                title=title,
                url=url,
                kind="video",
                poster=poster,
                duration=dur,
                source_page=base_url,
            ))
        return cards

    # ---------------- 单页/详情页快速解析 ----------------
    def single_source(self, url: str):
        try:
            return self.resolve_video(VideoCard(title="", url=url, kind="video"))
        except Exception as e:
            log.info("18mh single_source 失败 %s: %s", url, e)
            return None

    # ---------------- 详情页 -> m3u8 ----------------
    def resolve_video(self, card) -> VideoSource:
        url = card.url if hasattr(card, "url") else card["url"]
        title = getattr(card, "title", "") or (card.get("title", "") if isinstance(card, dict) else "")
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        html = fetch_text(url, headers={"Referer": origin})
        uniq = self._extract_m3u8s(html)
        if not uniq:
            raise ValueError("详情页未找到 m3u8 播放地址")
        best = uniq[0]
        alts = [
            {
                "url": u,
                "protocol": "hls",
                "quality": self._quality_of(u, html),
                "note": "备选地址",
            }
            for u in uniq[1:11]
        ]
        return VideoSource(
            url=best,
            title=title,
            protocol="hls",
            headers={
                "Referer": f"https://{HOST}/",
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
            note="18mh 防盗链 m3u8（带 auth_key）",
            quality=self._quality_of(best, html),
            alternatives=alts,
        )

    @staticmethod
    def _extract_m3u8s(html: str) -> list:
        """详情页里的 m3u8（JS/JSON 中反斜杠转义先还原），保序去重。"""
        text = html.replace(BS + "/", "/")
        out = []
        for m in _M3U8_RE.finditer(text):
            u = m.group(0).rstrip(BS)
            if u not in out:
                out.append(u)
        return out

    @staticmethod
    def _quality_of(url: str, html: str = "") -> str:
        # 只在 URL 里识别常见清晰度，避免被页面里 -9999px 之类的文本干扰
        m = _QUALITY_RE.search(url)
        return f"{int(m.group(1))}p" if m else ""