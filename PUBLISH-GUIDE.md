# 适配器发布与更新操作手册

> 用途：当 py-vd 需要**新增 / 修改 / 删除**站点适配器时，按本手册把新版适配器发布到更新仓库
> `DEXD177re0/js`，用户端在软件里点「检查更新 → 更新全部」即可生效，**不需要重新打包 EXE、不需要重启**。
> 本手册面向开发人员或 AI 助手，照着做即可，无需理解程序内部实现。

## 0. 原理（30 秒版）

- 更新仓库 `https://github.com/DEXD177re0/js`（公开仓库，分支 `main`）只放适配器文件 + `manifest.json` 清单。
- 清单记录每个文件的 `version` 和 `sha256`。
- py-vd 检查更新 = 拉清单，和本地 `data/adapter_versions.json` 比对**版本号**；版本不同才下载：
  下载 → sha256 校验 → 备份（.bak）→ 替换 → 热重载；失败自动回滚。
- **两条铁律：**
  1. 修改了文件内容但**没升 `version`** → 用户端显示「已是最新」，不会下载。
  2. **清单里没有的文件** → 程序根本不会下载它（新增文件必须加清单条目）。

## 1. 仓库结构（DEXD177re0/js，main 分支）

```
js/
├── manifest.json       ← 清单（每次发布必改）
├── site_18mh.py
├── site_91porna.py
├── site_generic.py
├── rules.json
├── README.md
└── .gitattributes      ← 内容固定为「* text eol=lf」，已统一 LF 行尾，别删别改
```

## 2. manifest.json 格式

```json
{
  "repo_version": 2,
  "adapters": [
    { "id": "18mh",    "file": "site_18mh.py",     "version": "1.0.0", "sha256": "50bcbdca..." },
    { "id": "91porna", "file": "site_91porna.py",  "version": "1.0.0", "sha256": "c484f371..." },
    { "id": "generic", "file": "site_generic.py",  "version": "1.0.0", "sha256": "51b002f8..." },
    { "id": "rules",   "file": "rules.json",       "version": "1.0.0", "sha256": "0fd0f007..." }
  ]
}
```

- `repo_version`：清单整体版本，每次发布 `+1`。
- `version`：该文件版本。**文件内容一变必须升号**（如 `1.0.0 → 1.0.1`）。
- `sha256`：该文件**提交后字节（LF 行尾）**的 SHA-256，必须用脚本计算，不要手写。

## 3. 修改已有适配器并发布（标准流程）

### 3.1 准备

```powershell
git clone https://github.com/DEXD177re0/js.git
cd js
```

仓库公开，无需 token。

### 3.2 修改适配器文件

改 `site_xxx.py` / `rules.json`（写适配器的接口见下文第 4 节）。注意：

- 文件必须是 **UTF-8 编码、LF 行尾**（仓库已固定 LF）。
- **不要在 PowerShell 里用管道把含中文的内容喂给 Python**（`@'...'@ | python -` 会把中文变成问号）。
  写文件一律用 `io.open(path, "w", encoding="utf-8", newline="\n")`。

### 3.3 升版本号 + 重算 sha256 + 更新清单（复制即用）

把下面脚本里的 `REPO`、`FILE`、`NEW_VERSION` 改成实际值后运行（支持「改已有文件」和「新增文件」两种场景）：

```python
import io, json, hashlib
from pathlib import Path

REPO = Path(r"C:\path\to\js")   # ← 改成你 clone 下来的仓库路径
FILE = "site_18mh.py"           # ← 改成你改/新增的文件名
NEW_VERSION = "1.0.1"           # ← 改成新版本号（如 1.0.1）

def norm_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")

p = REPO / FILE
data = norm_lf(p.read_bytes())
p.write_bytes(data)             # 工作区统一 LF，保证与提交后的字节一致
sha = hashlib.sha256(data).hexdigest()

mp = REPO / "manifest.json"
mani = json.loads(mp.read_text(encoding="utf-8"))
entry = next((a for a in mani["adapters"] if a["file"] == FILE), None)
if entry is None:               # 新增文件：自动加清单条目
    default_id = FILE.removeprefix("site_").removesuffix(".py")
    entry = {"id": default_id, "file": FILE, "version": NEW_VERSION, "sha256": sha}
    mani["adapters"].append(entry)
else:                           # 已有文件：升版本 + 换 sha256
    entry["version"] = NEW_VERSION
    entry["sha256"] = sha
mani["repo_version"] = mani.get("repo_version", 1) + 1
mp.write_text(json.dumps(mani, ensure_ascii=False, indent=2) + "\n",
              encoding="utf-8", newline="\n")
print(FILE, "->", NEW_VERSION, sha)
```

运行后确认输出里的 sha256 和清单里的一致。

### 3.4 提交推送

```powershell
git add -A
git commit -m "site_18mh.py 1.0.1: 修复 xxx"
git push origin main
```

如遇 SSL 握手失败（schannel 报错），重试加 `-c http.version=HTTP/1.1`：

```powershell
git -c http.version=HTTP/1.1 push origin main
```

### 3.5 发布后验证（推荐）

```powershell
python -c "import urllib.request,json; m=json.load(urllib.request.urlopen('https://raw.githubusercontent.com/DEXD177re0/js/main/manifest.json')); print('repo_version', m['repo_version']); [print(a['file'], a['version'], a['sha256']) for a in m['adapters']]"
```

确认目标文件的 `version` 已升、`sha256` 与 3.3 输出一致。

### 3.6 用户端生效

- 打开 py-vd →「🧩 适配器管理」→「检查更新」→ 看到 `v1.0.0 → v1.0.1` →「更新全部」。
- 出问题点「回滚上次」恢复。
- 注意：GitHub raw 有 CDN 缓存，刚推送后几分钟内可能拉到旧版；程序在 raw 失败时会自动走 GitHub API，一般不影响。

## 4. 新增一个站点适配器

1. 写 `site_你的网站.py`，实现 4 个方法（接口与内置适配器一致，完整示例见 `adapters/README.md`）：

```python
from urllib.parse import urlparse, urljoin
from parsel import Selector
from app.models import VideoCard, VideoSource

class SiteMySite:
    name = "mysite"   # 唯一名称

    def match(self, url: str) -> bool:
        return urlparse(url).netloc.endswith("mysite.com")

    def parse_cards(self, html: str, base_url: str) -> list:
        # 列表页：HTML -> VideoCard 列表
        ...

    def resolve_video(self, card) -> VideoSource:
        # 详情页：返回真实可下载的 m3u8 / mp4
        ...
```

2. 把文件放进 js 仓库，运行 3.3 的脚本（`FILE` 填新文件名，脚本会自动加清单条目）。
3. 按 3.4 / 3.5 提交推送并验证。
4. 用户端「检查更新 → 更新全部」后，适配器列表会出现新站点。

## 5. 删除 / 停用适配器

- 从 js 仓库删掉该文件，并从 `manifest.json` 的 `adapters` 里移除对应条目，提交推送。
- 用户端「更新全部」只处理清单里列出的文件；用户本地已下载的旧文件**不会自动删除**（设计如此）。
  如需彻底移除，让用户在本地 `adapters\` 目录里手动删掉对应文件（会自动热重载）。

## 6. 常见坑速查表

| 坑 | 现象 | 解决 |
|---|---|---|
| 改了内容没升 version | 用户端显示「已是最新」 | 改内容必须升 `version` |
| sha256 按 CRLF 字节算 | 更新时「校验失败(sha256 不匹配)」 | 用 3.3 脚本按 LF 字节算（脚本已处理） |
| PowerShell 管道写中文 | 文件里中文变 `??` | 写文件用 Python `io.open(..., encoding="utf-8")` |
| raw CDN 缓存 | 刚推送几分钟内拉到旧版 | 稍等或再点一次「检查更新」；程序有 API 兜底 |
| 新文件没进 manifest | 用户端不显示、不下载 | 必须加清单条目（3.3 脚本自动加） |
| `removeprefix` 报错 | Python < 3.9 | 改用 `FILE[5:] if FILE.startswith("site_") else FILE` 之类写法 |

## 7. 一次发布完成的核对清单

- [ ] `manifest.json`：目标文件 `version` 已升、`sha256` 已更新、`repo_version` +1
- [ ] 推送成功到 `DEXD177re0/js` 的 `main` 分支
- [ ] 3.5 验证命令输出正确（raw 拉到的版本号 / sha256 与本地一致）
- [ ] 用户端「检查更新」能看到版本变化、「更新全部」后列表显示新版本
- [ ] （可选）真实验证一次目标网站能正常分析 / 解析出视频

## 8. 相关位置

- 更新源默认地址：`https://raw.githubusercontent.com/DEXD177re0/js/main`（软件设置里可改）
- 本地版本记录：`data\adapter_versions.json`（用户端程序自动维护，别手动删；删了会当成全新文件重新下载）
- 更新逻辑代码：`app/core/adapter_updater.py`；接口：`/api/adapters/update-check`、`/api/adapters/update`、`/api/adapters/rollback`
