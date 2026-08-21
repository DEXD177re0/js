# -*- coding: utf-8 -*-
"""kuaimsp 站点适配器。

链路：分类/列表/搜索页 -> 视频卡片（li.tp5-section-content__item，广告卡片 tp5-module-ad 跳过）
     -> 详情页 window.__ARCHIVE_PLAYER__ JSON -> cdnLine + rawPath = 真实 m3u8（AES-128，CDN 域名）。

翻页：路径式 /category/cate69/2/（?page= 无效），分页导航 #pagination-nav .tp5-pagination。
"""
import json
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse

from parsel import Selector

from app.config import DEFAULT_HEADERS
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

log = logging.getLogger(__name__)

HOST = "kuaimsp.net"
_PLAYER_RE = re.compile(r"window\.__ARCHIVE_PLAYER__\s*=\s*(\{.*?\})\s*;", re.S)
_PAGE_NUM_RE = re.compile(r"/(\d{1,5})$")


class SiteKuaimsp:
    name = "kuaimsp"

    def match(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host == HOST or host.endswith("." + HOST)

    # ---------------- 列表/搜索页卡片 ----------------
    def parse_cards(self, html: str, base_url: str) -> list:
        sel = Selector(text=html)
        cards = []
        for item in sel.css("li.tp5-section-content__item:not(.tp5-module-ad)"):
            a = item.css('a[href*="/video/"]')
            href = (a.attrib.get("href") or "").strip() if a else ""
            if not href:
                continue
            img = item.css("img")
            if img:
                title = (img.attrib.get("alt") or "").strip()
                poster = img.attrib.get("data-src") or img.attrib.get("src") or ""
            else:
                title = " ".join(item.css("h3 ::text, .title ::text").getall()).strip()
                poster = ""
            card = VideoCard(
                title=title,
                url=urljoin(base_url, href),
                kind="video",
                poster=poster,
                duration=self._fmt_duration(item.css(".tp5-duration::text").get() or ""),
                source_page=base_url,
            )
            plink = (item.css(".tp5-item-preview::attr(data-plink)").get() or "").strip()
            if plink:
                card.preview_path = plink
            cards.append(card)
        return cards

    # ---------------- 单页/详情页快速解析 ----------------
    def single_source(self, url: str):
        try:
            return self.resolve_video(VideoCard(title="", url=url, kind="video"))
        except Exception as e:  # noqa: BLE001
            log.info("kuaimsp single_source 失败 %s: %s", url, e)
            return None

    # ---------------- 详情页 -> m3u8 ----------------
    def resolve_video(self, card) -> VideoSource:
        card_url = card.url if hasattr(card, "url") else card["url"]
        title = getattr(card, "title", "") or (
            card.get("title", "") if isinstance(card, dict) else ""
        )
        html = fetch_text(card_url, headers={"Referer": f"https://{HOST}/"})
        m = _PLAYER_RE.search(html)
        if not m:
            raise ValueError("详情页未找到 __ARCHIVE_PLAYER__ 播放配置")
        data = json.loads(m.group(1))
        cdn = (data.get("cdnLine") or "").strip().rstrip("/")
        raw = (data.get("rawPath") or "").strip()
        if not cdn or not raw:
            raise ValueError("播放配置缺少 cdnLine / rawPath")
        main = cdn + raw
        alts = []
        plink = getattr(card, "preview_path", "") or ""
        if plink:
            alts.append(
                {
                    "url": cdn + plink,
                    "protocol": "hls",
                    "quality": "",
                    "note": "预览流（时长较短，主源不可用时备用）",
                }
            )
        return VideoSource(
            url=main,
            title=title,
            protocol="hls",
            headers={
                "Referer": f"https://{HOST}/",
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
            note="kuaimsp 主 m3u8（AES-128，CDN 域名）",
            quality="",
            alternatives=alts,
        )

    # ---------------- 路径式翻页 ----------------
    @staticmethod
    def page_url(url: str, page: int) -> str:
        """/category/cate69/ -> /category/cate69/2/；已带页码则原位替换。"""
        parts = urlparse(url)
        path = parts.path.rstrip("/")
        m = _PAGE_NUM_RE.search(path)
        if m:
            path = path[: m.start()] + "/" + str(page)
        else:
            path = path + "/" + str(page)
        return urlunparse(parts._replace(path=path + "/"))

    @staticmethod
    def _fmt_duration(raw: str) -> str:
        """tp5-duration 是秒数（如 2462），转成 MM:SS / H:MM:SS。"""
        raw = (raw or "").strip()
        if raw.isdigit():
            s = int(raw)
            h, rem = divmod(s, 3600)
            m, sec = divmod(rem, 60)
            return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        return raw
