# 外部适配器目录

这里放「站点适配器插件」和「通用适配规则」，改完在网页里点「🧩 适配器管理 → 重新加载适配器」即可生效，**不需要重启服务、不需要重新打包**。

- `site_generic.py`：通用兜底适配器（默认自带，**不要删除**，所有网站的兜底靠它）
- `site_xxx.py`：其它站点适配器插件（按需新建）
- `rules.json`：通用适配器的启发式规则（可选，默认用代码内置值）

所有适配器都是这里的外部文件，改完会自动热重载（也可手动点「重新加载适配器」）。

## 添加一个站点插件

新建 `site_你的网站.py`，实现下面 4 个方法即可（接口和现有适配器完全一样）：

```python
from urllib.parse import urljoin, urlparse
from parsel import Selector
from app.models import VideoCard, VideoSource


class SiteMySite:
    name = "mysite"                       # 唯一名称，同名会覆盖现有适配器

    def match(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("mysite.com")

    # 列表页：把页面 HTML 解析成一张张视频卡片
    def parse_cards(self, html: str, base_url: str) -> list:
        sel = Selector(text=html)
        cards = []
        for item in sel.css(".video-item"):
            href = item.css("a::attr(href)").get()
            if not href:
                continue
            title = (item.css("img::attr(alt)").get() or "").strip()
            cards.append(VideoCard(
                title=title, url=urljoin(base_url, href),
                kind="video", source_page=base_url,
            ))
        return cards

    # 详情页：拿到真实可下载的视频地址（m3u8 / mp4）
    def resolve_video(self, card) -> VideoSource:
        # card.url 是详情页；抓页面、找 og:video / video 标签 / JS 里的 m3u8……
        # 也可以直接复用通用提取：
        from app.core.sites.site_generic import GenericSite
        return GenericSite().resolve_video(card)
```

保存后点「重新加载适配器」，列表里会出现 `mysite · 外部插件`。

## 通用规则 rules.json

通用适配器（generic）的启发式都在代码里有默认值；想不写代码微调某个站时，新建/编辑 `rules.json`：

```json
{
  "skip_path_words": ["/login", "/profile"],
  "skip_host_re": "facebook|instagram|help\\.",
  "detail_hint_extra": ["/watch-video"],
  "poster_attrs": ["data-src", "src"],
  "direct_attrs": ["data-url", "data-video"],
  "sites": {
    "eporner.com": {
      "card": "a[href*='/video-']",
      "title": "img@alt",
      "poster": "img@src"
    }
  }
}
```

`sites` 里的键是域名片段，命中后直接按 `card` 这个 CSS 选择器抽卡片，
`title` / `poster` 支持 `img@alt`（属性）或 `.title`（元素文本）写法，多个用 `|` 分隔取第一个有值的。

## 如何发布更新

适配器文件 + manifest.json 的发布/更新/验证完整流程见 **[PUBLISH-GUIDE.md](PUBLISH-GUIDE.md)**（修改适配器、新增站点、删适配器、常见坑、核对清单都有）。
