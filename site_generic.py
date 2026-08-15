"""通用站点适配器（兜底策略）：任意网页尽力识别视频。

识别链路：
1. 直接视频地址（.m3u8 / .mp4 / .webm / .mov 等） -> 直接用
2. yt-dlp 能识别的站点（YouTube、Bilibili、Twitter/X 等） -> 用 yt-dlp
3. 普通网页 -> 提取 og:video / video 标签 / JS 配置里的 m3u8、mp4 / 页面内视频链接
4. 列表页 -> 识别视频卡片（<a><img>、背景图、懒加载 data-* 均支持），再逐卡解析详情页

用法：作为 ADAPTERS 列表最后一个兜底，任何 URL 都 match。
"""
import html as html_mod
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

import yt_dlp
from parsel import Selector

from app import settings
from app.config import DEFAULT_HEADERS
from app.core.http import fetch_text
from app.models import VideoCard, VideoSource

log = logging.getLogger(__name__)

_VIDEO_EXT = r"(?:m3u8|mp4|webm|mov|mkv|flv)"
_VIDEO_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:" + _VIDEO_EXT + r")(?:[?#][^\s\"'<>\\]*)?", re.I
)
_RELATIVE_VIDEO_RE = re.compile(
    r"""["']((?:\.{0,2}/[^"'<>]*?)?\.(?:""" + _VIDEO_EXT + r""")(?:[?#][^"'<>]*)?)["']""", re.I
)
_JS_FIELD_RE = re.compile(
    r"""["'](?P<key>file|url|src|video|video_url|play_url|playurl|hls_url|m3u8|mp4|media_url|contentUrl|source)["']\s*[:=]\s*["'](?P<val>[^"']+?\.(?:(?:m3u8|mp4|webm|mov|mkv|flv))(?:[?#][^"']*)?)["']""",
    re.I,
)
_SKIP_URL_RE = re.compile(
    r"\.(?:css|js|png|jpe?g|gif|webp|svg|ico|json|xml|zip|rar)(?:[?#]|$)", re.I
)
_CLASS_HINT_RE = re.compile(
    r"(?:video|item|card|thumb|media|movie|vod|post|preview|entry|cover|preview)",
    re.I,
)
_SKIP_ANCESTOR_RE = re.compile(
    r"nav|menu|header|footer|toolbar|breadcrumb|tooltip|pagination|search|filter|sidebar|topbar|banner|logo",
    re.I,
)
_DETAIL_PAGE_RE = re.compile(
    r"og:video|twitter:player|<video\b|<iframe[^>]+(?:player|embed)", re.I
)


# ---------------- 规则（可用 adapters/rules.json 覆盖，热重载） ----------------
DEFAULT_RULES = {
    "skip_path_words": (
        "/login", "/register", "/signin", "/signup", "/logout",
        "/search", "/tags", "/tag/", "/category", "/feed", "/rss",
        "/previews", "/rankings",
        "/profile", "/pornstar", "/categories", "/channel", "/model",
        "/blog", "/help", "/member", "/browse", "/user",
    ),
    "skip_host_re": (
        r"facebook|instagram|google|bing|baidu|360\.com|so\.com|w3\.org"
        r"|schema\.org|example\.com|help\.|support\."
    ),
    "detail_hint_extra": [],
    "poster_attrs": (
        "data-src", "data-original", "data-lazy-src", "data-echo",
        "data-poster", "data-thumb", "data-background-image", "data-bg",
        "data-srcset", "src",
    ),
    "direct_attrs": (
        "data-url", "data-video", "data-src", "data-href",
        "data-mp4", "data-file", "data-hls", "data-source",
    ),
    "sites": {},
}
RULES = {}
SITE_RULES = {}
_DETAIL_HINT_RE = None
_SKIP_HOST_RE = None
_SKIP_PATH_WORDS = ()
_POSTER_ATTRS = ()
_DIRECT_ATTRS = ()


def _compile_rules():
    global _DETAIL_HINT_RE, _SKIP_HOST_RE, _SKIP_PATH_WORDS
    global _POSTER_ATTRS, _DIRECT_ATTRS, SITE_RULES
    _SKIP_PATH_WORDS = tuple(RULES.get("skip_path_words") or ())
    _SKIP_HOST_RE = re.compile("(?:%s)" % (RULES.get("skip_host_re") or ""), re.I)
    extra = RULES.get("detail_hint_extra") or []
    extra_part = ("|" + "|".join(re.escape(str(x)) for x in extra)) if extra else ""
    _DETAIL_HINT_RE = re.compile(
        r"(?:/video/|/watch|/play(?:er)?/|/detail/|/view/|/show/|/movie|/vod|/episode|/av\d+|video_key|id=\d+"
        + extra_part
        + r"|/video-[A-Za-z0-9_-]{6,}"
        + r"|/\d+(?:\.html?)?(?:[?#]|$))",
        re.I,
    )
    _POSTER_ATTRS = tuple(RULES.get("poster_attrs") or ())
    _DIRECT_ATTRS = tuple(RULES.get("direct_attrs") or ())
    SITE_RULES = RULES.get("sites") or {}


def apply_rules(data):
    """用 rules.json 数据覆盖默认规则；data 为 None/{} 时恢复默认。"""
    rules = dict(DEFAULT_RULES)
    if isinstance(data, dict):
        for k in ("skip_path_words", "skip_host_re", "detail_hint_extra",
                  "poster_attrs", "direct_attrs", "sites"):
            if k in data:
                rules[k] = data[k]
    RULES.clear()
    RULES.update(rules)
    _compile_rules()


apply_rules(None)


# ---------------- 工具 ----------------
def is_video_url(url):
    return bool(re.search(r"\.(?:" + _VIDEO_EXT + r")(?:[?#]|$)", url, re.I))


def _protocol(url):
    return "hls" if re.search(r"\.m3u8(?:[?#]|$)", url, re.I) else "http"


def _clean(raw):
    u = html_mod.unescape(str(raw).strip())
    u = u.replace("\\/", "/")
    return u.rstrip(".,;'\")]}")


def _abs(raw, base_url):
    u = _clean(raw)
    if not u or u.startswith(("data:", "blob:")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith(("http://", "https://")):
        u = urljoin(base_url, u)
    return u


def _origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _origin_headers(url):
    return {"Referer": _origin(url), "User-Agent": DEFAULT_HEADERS["User-Agent"]}


def _title_from_url(url):
    try:
        p = urlparse(url)
        seg = [s for s in p.path.split("/") if s]
        name = unquote(seg[-1]) if seg else ""
        name = re.sub(r"\.(m3u8|mp4)$", "", name, flags=re.I)
        name = re.sub(r"[_-]+", " ", name).strip()
        return (name or p.netloc)[:120]
    except Exception:
        return url


def _page_title(html_text):
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    if m:
        t = re.sub(r"\s+", " ", html_mod.unescape(m.group(1))).strip()
        if t:
            return t[:200]
    return ""



def _ancestor_skip_hint(a):
    """航导/页脚/工具栏等容器里的链接不当卡片。"""
    node = a
    for _ in range(3):
        parent = node.xpath("..")
        if not parent:
            break
        node = parent[0]
        cls = node.attrib.get("class", "")
        if isinstance(cls, list):
            cls = " ".join(cls)
        if cls and _SKIP_ANCESTOR_RE.search(cls):
            return True
    return False


def _ancestor_class_hint(a):
    node = a
    for _ in range(4):
        cls = node.attrib.get("class", "")
        if isinstance(cls, list):
            cls = " ".join(cls)
        if cls and _CLASS_HINT_RE.search(cls):
            return True
        parent = node.xpath("..")
        if not parent:
            break
        node = parent[0]
    return False


def _img_poster(img):
    """从 <img> 各懒加载属性里取真实海报地址。"""
    for attr in _POSTER_ATTRS:
        v = img.attrib.get(attr, "")
        if not v:
            continue
        if attr == "data-srcset":
            v = v.split(",")[0].strip().split(" ")[0]
        if v.startswith(("data:", "blob:", "#")):
            continue
        return v
    return ""


def _site_rule_for(url):
    """按域名匹配 rules.json 里 sites 的卡片规则。"""
    host = urlparse(url).netloc.lower()
    for key, rule in SITE_RULES.items():
        if not isinstance(rule, dict):
            continue
        if key in host or host in key:
            return rule
    return None


def _expr_text(node, expr):
    """按 img@alt | .title 这类表达式取文本，取第一个有值的。"""
    for part in expr.split("|"):
        part = part.strip()
        if not part:
            continue
        if "@" in part:
            css, attr = part.rsplit("@", 1)
            el = node.css(css)
            if el and el[0].attrib.get(attr, ""):
                return el[0].attrib[attr].strip()
        else:
            t = " ".join(x.strip() for x in node.css(part + " ::text").getall() if x.strip())
            if t:
                return t
    return ""


def _expr_attr(node, expr):
    """按 img@src 这类表达式取属性值（海报），忽略 data:/blob:。"""
    for part in expr.split("|"):
        part = part.strip()
        if not part or "@" not in part:
            continue
        css, attr = part.rsplit("@", 1)
        el = node.css(css)
        if el:
            v = el[0].attrib.get(attr, "")
            if v and not v.startswith(("data:", "blob:")):
                return v.strip()
    return ""


# ---------------- 清晰度 ----------------
def _quality_label(height=0, url="", extra=""):
    """从高度/URL/格式串里提取清晰度标签，如 1080p。"""
    if height:
        return f"{int(height)}p"
    m = re.search(r"(\d{3,4})p", f"{url} {extra}", re.I)
    return f"{int(m.group(1))}p" if m else ""


# ---------------- yt-dlp ----------------
def ytdlp_formats(url):
    """用 yt-dlp 提取可用格式；失败返回 None。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.info("yt-dlp 无法识别 %s: %s", url, e)
        return None
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]
    fmts = info.get("formats") or []
    if not fmts and info.get("url"):
        fmts = [info]
    out = []
    title = info.get("title") or url
    for f in fmts:
        furl = f.get("url")
        if not furl:
            continue
        proto = f.get("protocol") or ""
        kind = "hls" if ("m3u8" in proto or "m3u8" in furl) else "http"
        out.append({
            "url": furl,
            "title": title,
            "protocol": kind,
            "headers": f.get("http_headers") or {},
            "height": f.get("height") or 0,
            "format": f.get("format") or f.get("format_id") or "",
        })
    return out or None


def _pick_format(fmts):
    """按设置选择清晰度：best 取最高；指定 720p/1080p 等时选最接近且不超限的。"""
    target = settings.get("quality", "best")
    m = re.search(r"(\d+)", str(target))
    if m:
        th = int(m.group(1))
        with_h = [f for f in fmts if (f.get("height") or 0) >= 1]
        if with_h:
            exact = [f for f in with_h if int(f.get("height") or 0) == th]
            pool = exact or [f for f in with_h if int(f.get("height") or 0) <= th] or with_h
            return min(pool, key=lambda f: (
                abs(int(f.get("height") or 0) - th),
                0 if f.get("protocol") == "hls" else 1,
            ))
    def key(f):
        return (f.get("height") or 0, 1 if f.get("protocol") == "hls" else 0)
    return max(fmts, key=key)


def ytdlp_entries(url, limit=12):
    """yt-dlp 提取播放列表条目（剧集/多P），每集取条目链接；非列表或失败返回 None。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "extract_flat": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info or info.get("_type") != "playlist":
        return None
    out = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        u = e.get("url") or e.get("webpage_url")
        if not u:
            continue
        out.append({
            "url": u,
            "protocol": "hls" if "m3u8" in str(u).lower() else "http",
            "quality": "",
            "note": "分集：" + (e.get("title") or ""),
        })
        if len(out) >= limit:
            break
    return out or None


def _fmt_to_source(fmts):
    best = _pick_format(fmts)
    note = "yt-dlp 识别"
    if best.get("format"):
        note += " · " + best["format"]
    return VideoSource(
        url=best["url"],
        title=best.get("title") or "",
        protocol=best["protocol"],
        headers=best.get("headers") or {},
        note=note,
        quality=_quality_label(best.get("height") or 0, best.get("url") or "", best.get("format") or ""),
    )


# ---------------- HTML 提取 ----------------
def extract_sources(html_text, base_url):
    """从详情页 HTML 提取候选视频源，按可信度排序返回。"""
    out = []
    seen = set()
    sel = Selector(text=html_text)

    def add(raw, note, title=""):
        u = _abs(raw, base_url)
        if not u or not is_video_url(u) or u in seen:
            return
        seen.add(u)
        out.append({
            "url": u,
            "protocol": _protocol(u),
            "note": note,
            "title": title,
            "quality": _quality_label(0, u, note),
        })

    for xp in (
        '//meta[@property="og:video"]/@content',
        '//meta[@property="og:video:url"]/@content',
        '//meta[@property="og:video:secure_url"]/@content',
        '//meta[@property="twitter:player:stream"]/@content',
        '//meta[@name="twitter:player:stream"]/@content',
    ):
        for v in sel.xpath(xp).getall():
            add(v, "og/twitter")
    for attr in ("src", "data-src", "data-url", "data-source", "data-video", "data-file", "data-hls"):
        for node in sel.css("video, video source, source, audio, audio source"):
            v = node.attrib.get(attr, "")
            if v:
                add(v, "video 标签")
    for xp in (
        '//meta[@itemprop="contentUrl"]/@content',
        '//link[@itemprop="contentUrl"]/@href',
    ):
        for v in sel.xpath(xp).getall():
            add(v, "结构化数据")
    text = html_text.replace("\\/", "/")
    for m in _JS_FIELD_RE.finditer(text):
        add(m.group("val"), "JS 配置")
    for m in _RELATIVE_VIDEO_RE.finditer(text):
        add(m.group(1), "相对路径")
    for m in _VIDEO_URL_RE.finditer(text):
        add(m.group(0), "")
    return out


def _res_of(s):
    m = re.search(r"(\d{3,4})p", f"{s.get('url', '')} {s.get('note', '')}")
    return int(m.group(1)) if m else 0


def _pick_best(srcs):
    """按可信度 + 清晰度设置选最优源。"""
    weights = {"JS 配置": 6, "og/twitter": 4, "结构化数据": 4, "video 标签": 3, "相对路径": 2, "": 1}
    target = settings.get("quality", "best")
    m = re.search(r"(\d+)", str(target))
    if m:
        th = int(m.group(1))
        with_res = [s for s in srcs if _res_of(s) >= 1]
        if with_res:
            exact = [s for s in with_res if _res_of(s) == th]
            pool = exact or [s for s in with_res if _res_of(s) <= th] or with_res

            def key2(s):
                return (
                    1 if s["protocol"] == "hls" else 0,
                    weights.get(s.get("note", ""), 1),
                    -abs(_res_of(s) - th),
                )
            return max(pool, key=key2)

    def key(s):
        return (1 if s["protocol"] == "hls" else 0, weights.get(s.get("note", ""), 1), _res_of(s))
    return max(srcs, key=key)


# ---------------- 适配器 ----------------
class GenericSite:
    name = "generic"

    def match(self, url: str) -> bool:
        return True

    # -------- 单页/详情页快速解析（analyzer 在 mode=single 时优先调用） --------
    def single_source(self, url: str):
        """识别单视频页；无法识别返回 None。"""
        try:
            if is_video_url(url):
                return VideoSource(
                    url=url, title=_title_from_url(url), protocol=_protocol(url),
                    headers=_origin_headers(url), note="直接视频地址",
                )
            fmts = ytdlp_formats(url)
            if fmts:
                return _fmt_to_source(fmts)
            html_text = fetch_text(url, headers={"Referer": _origin(url)})
            return self._extract_from_html(url, html_text, "")
        except Exception as e:
            log.info("generic single_source 失败 %s: %s", url, e)
        return None

    # -------- 列表/详情页卡片提取 --------
    def parse_cards(self, html, base_url) -> list:
        head = html[:2048].lstrip()
        if "<" not in head:
            # 直接就是 m3u8 / mp4 内容（播放列表或二进制转文本）
            return [VideoCard(
                title=_title_from_url(base_url), url=base_url,
                kind="video", source_page=base_url,
            )]
        rule = _site_rule_for(base_url)
        if rule:
            cards = self._cards_by_rule(html, base_url, rule)
            if cards:
                return cards
        cards, seen = [], set()

        def add(url, title="", poster="", dur=""):
            u = _abs(url, base_url)
            if not u or u in seen or not is_video_url(u):
                return
            seen.add(u)
            cards.append(VideoCard(
                title=title, url=u, kind="video",
                poster=poster, duration=dur, source_page=base_url,
            ))

        sel = Selector(text=html)
        # og:video / twitter:player:stream（详情页主视频）
        for xp in (
            '//meta[@property="og:video"]/@content',
            '//meta[@property="og:video:url"]/@content',
            '//meta[@property="og:video:secure_url"]/@content',
            '//meta[@property="twitter:player:stream"]/@content',
            '//meta[@name="twitter:player:stream"]/@content',
        ):
            for v in sel.xpath(xp).getall():
                add(v, _title_from_url(v))
        # video / source 标签（含懒加载 data-src）
        for attr in ("src", "data-src", "data-url", "data-source", "data-video", "data-file"):
            for node in sel.css("video, video source, source"):
                v = node.attrib.get(attr, "")
                if v:
                    add(v, _title_from_url(v))
        # <a> 视频卡片（列表页）
        for card in self._anchor_cards(html, base_url):
            if card.url not in seen:
                seen.add(card.url)
                cards.append(card)
        # 页面里散落的 m3u8 / mp4 绝对链接
        for m in _VIDEO_URL_RE.finditer(html.replace("\\/", "/")):
            u = _clean(m.group(0))
            if is_video_url(u) and u not in seen:
                seen.add(u)
                cards.append(VideoCard(
                    title=_title_from_url(u), url=u, kind="video", source_page=base_url,
                ))
        # 一张卡都没识别到，但页面像详情页 -> 整页作为一张卡
        if not cards and self._looks_like_detail_page(html):
            cards.append(VideoCard(
                title=_page_title(html) or _title_from_url(base_url),
                url=base_url, kind="video", source_page=base_url,
            ))
        return cards

    @staticmethod
    def _cards_by_rule(html, base_url, rule):
        """按 rules.json 站点规则直接抽卡片（CSS 选择器快路径）。"""
        sel = Selector(text=html)
        selector = (rule.get("card") or "").strip() or "a[href]"
        title_expr = (rule.get("title") or "").strip()
        poster_expr = (rule.get("poster") or "").strip()
        out, seen = [], set()
        for a in sel.css(selector):
            href = (a.attrib.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:")):
                continue
            url = _abs(href, base_url)
            if not url or url in seen:
                continue
            seen.add(url)
            title = _expr_text(a, title_expr) if title_expr else ""
            if not title:
                title = (a.attrib.get("title") or "").strip()
            if not title:
                title = _title_from_url(url)
            poster = _expr_attr(a, poster_expr) if poster_expr else ""
            out.append(VideoCard(
                title=title, url=url, kind="video",
                poster=poster, source_page=base_url,
            ))
        return out

    @staticmethod
    def _looks_like_detail_page(html):
        return bool(_DETAIL_PAGE_RE.search(html)) or bool(re.search(r"\.m3u8", html, re.I))

    @staticmethod
    def _anchor_cards(html, base_url):
        sel = Selector(text=html)
        scored = {}
        for a in sel.css("a[href]"):
            href = a.attrib.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = _abs(href, base_url)
            if not url or not url.startswith(("http://", "https://")):
                continue
            path = urlparse(url).path
            if _SKIP_URL_RE.search(path):
                continue
            if _SKIP_HOST_RE.search(urlparse(url).netloc):
                continue
            if _ancestor_skip_hint(a):
                continue
            low = url.lower()
            if any(w in low for w in _SKIP_PATH_WORDS):
                continue
            if url.rstrip("/") == base_url.rstrip("/"):
                continue
            segs = [s for s in path.split("/") if s]
            if len(segs) <= 1 and not _DETAIL_HINT_RE.search(url):
                # 顶部导航/频道链接（如 /、/comic、/mv）不当卡片，除非有图片或直接视频
                has_img = bool(a.css("img"))
                direct_here = any(a.attrib.get(attr, "") for attr in _DIRECT_ATTRS)
                if not has_img and not direct_here:
                    continue
            # 锚点自身 data-* 里直接是视频地址
            direct = ""
            for attr in _DIRECT_ATTRS:
                v = a.attrib.get(attr, "")
                av = _abs(v, base_url)
                if av and is_video_url(av):
                    direct = av
                    break
            img = a.css("img")
            poster, alt = "", ""
            if img:
                im = img[0]
                poster = _img_poster(im)
                alt = (im.attrib.get("alt") or im.attrib.get("title") or "").strip()
            else:
                # 无 <img> 的卡片：background-image / data-bg 海报
                for attr in ("style", "data-bg", "data-background", "data-background-image"):
                    v = a.attrib.get(attr, "")
                    if attr == "style":
                        m = re.search(r"background(?:-image)?\s*:\s*url\(['\"]?([^)'\"]+)", v, re.I)
                        v = m.group(1) if m else ""
                    if v and not v.startswith(("data:", "blob:")):
                        poster = v
                        break
            a_text = " ".join(a.xpath(".//text()").getall()).strip()
            title = alt or ""
            if not title:
                t = a.css(
                    ".title ::text, .name ::text, [class*=title] ::text, "
                    "h3 ::text, h4 ::text, h5 ::text, strong ::text"
                ).getall()
                t = " ".join(x.strip() for x in t if x.strip())
                title = t
            if not title:
                title = a_text or (a.attrib.get("title") or "").strip()
            if not title:
                title = _title_from_url(url)
            score = 0
            if direct:
                score += 3
            if _DETAIL_HINT_RE.search(url):
                score += 2
            if _ancestor_class_hint(a):
                score += 1
            if img and alt:
                score += 1
            if len(segs) >= 2:
                score += 1
            if a_text and len(a_text) <= 80:
                score += 1
            if score < 2:
                continue
            # 要么是直接视频地址，要么看得出是详情页/卡片类名，避免把 logo/about 等链接当卡片
            if not (direct or _DETAIL_HINT_RE.search(url) or _ancestor_class_hint(a)):
                continue
            prev = scored.get(url)
            if prev and prev[0] >= score:
                continue
            scored[url] = (score, title, poster, direct)
        out = []
        for url, (_score, title, poster, direct) in scored.items():
            out.append(VideoCard(
                title=title, url=direct or url, kind="video",
                poster=poster, source_page=base_url,
            ))
        return out

    # -------- 卡片 -> 视频地址 --------
    def resolve_video(self, card) -> VideoSource:
        url = card.url if hasattr(card, "url") else card["url"]
        title = ""
        if hasattr(card, "title"):
            title = card.title or ""
        elif isinstance(card, dict):
            title = card.get("title") or ""
        if is_video_url(url):
            return VideoSource(
                url=url, title=title, protocol=_protocol(url),
                headers=_origin_headers(url), note="直接视频地址",
            )
        fmts = ytdlp_formats(url)
        if fmts:
            src = _fmt_to_source(fmts)
            src.title = title or src.title
            entries = ytdlp_entries(url)
            if entries:
                src.alternatives = entries
            return src
        origin = _origin(url)
        html_text = fetch_text(url, headers={"Referer": origin})
        src = self._extract_from_html(url, html_text, title)
        if src is None:
            raise ValueError("未找到可下载的视频地址（页面没有 m3u8/mp4）")
        return src

    @staticmethod
    def _build_source(srcs, page_url, html_text, title, origin, note):
        """从候选源列表生成 VideoSource：主源为最优，其余不同地址进 alternatives（多清晰度/多P）。"""
        best = _pick_best(srcs)
        alts, seen = [], {best["url"]}
        for s in srcs:
            u = s.get("url") or ""
            if not u or u in seen:
                continue
            seen.add(u)
            if len(alts) >= 10:      # 最多带 10 个备选地址，避免列表过长
                break
            alts.append({
                "url": u,
                "protocol": s.get("protocol") or _protocol(u),
                "quality": _quality_label(0, u, s.get("note") or ""),
                "note": s.get("note") or "备选地址",
            })
        return VideoSource(
            url=best["url"],
            title=title or _page_title(html_text),
            protocol=best["protocol"],
            headers={"Referer": origin, "User-Agent": DEFAULT_HEADERS["User-Agent"]},
            note=note,
            quality=_quality_label(0, best.get("url") or "", best.get("note") or ""),
            alternatives=alts,
        )

    @staticmethod
    def _extract_from_html(page_url, html_text, title=""):
        """从网页 HTML 提取视频；必要时再抓一层播放器 iframe。"""
        origin = _origin(page_url)
        srcs = extract_sources(html_text, page_url)
        if srcs:
            return GenericSite._build_source(
                srcs, page_url, html_text, title, origin, "网页提取"
            )
        for iframe in Selector(text=html_text).css("iframe[src], embed[src], object[data]"):
            fsrc = iframe.attrib.get("src") or iframe.attrib.get("data") or ""
            fabs = _abs(fsrc, page_url)
            if not fabs or not fabs.startswith(("http://", "https://")):
                continue
            if not (re.search(r"(?:player|embed|video)", fabs, re.I) or fabs.startswith(origin)):
                continue
            try:
                fhtml = fetch_text(fabs, headers={"Referer": origin})
            except Exception:
                continue
            srcs2 = extract_sources(fhtml, fabs)
            if srcs2:
                return GenericSite._build_source(
                    srcs2, fabs, html_text, title, origin, "播放器 iframe 提取"
                )
        # og:video 指向 HTML 播放页（非直链视频）时，跟进抓取
        for meta in Selector(text=html_text).css('meta[property^="og:video"], meta[name^="og:video"]'):
            v = (meta.attrib.get("content") or "").strip()
            if not v.startswith(("http://", "https://")) or is_video_url(v):
                continue
            try:
                fhtml = fetch_text(v, headers={"Referer": origin})
            except Exception:
                continue
            srcs3 = extract_sources(fhtml, v)
            if srcs3:
                return GenericSite._build_source(
                    srcs3, v, html_text, title, origin, "og:video 播放页提取"
                )
        return None
