# `server/static/lib/` 第三方前端库说明（不入库，首次运行拉取）

`server/static/lib/` 下的 5 个第三方前端库**故意不进 git**：合计约 3.5MB，光
`mermaid.min.js` 就 3.3MB，入库会成为永久历史负担（作者 2026-11-15 明确）。

它们现在被根 `.gitignore` 的 `lib/` 规则（无锚定，本意是忽略 Python 打包产物）一并忽略，
所以**新克隆 / 新机器 / 公开发布版首次运行是缺文件的**。缺了不会报错，只会静默降级：

| 缺失文件 | 前端表现 |
|---|---|
| `marked.min.js` | 对话内容不渲染 markdown，退化为纯文本 + 换行 |
| `highlight.min.js` | 代码块没有语法高亮 |
| `mermaid.min.js` | 对话里的 mermaid 流程图不渲染（`chat-markdown.js` 静默放弃） |
| `html-to-image.js` | 对话 / 短线面板「导出图片」失败（提示"导出依赖未就绪"） |
| `github-dark.min.css` | 代码块深色配色丢失（不影响功能） |

## 一键补齐（需要联网）

```bash
# Windows
.\venv\Scripts\python.exe scripts\ops\fetch_static_libs.py

# macOS / Linux
python scripts/ops/fetch_static_libs.py
```

脚本按下面表格里的**锁定版本**从 CDN 下载，逐字节 sha256 校验；已存在且校验通过就跳过
（幂等，可反复执行），`--force` 强制重下。

```bash
# 只体检、不联网（CI / 服务启动自检可用；有缺失时退出码 1）
python scripts/ops/fetch_static_libs.py --check

# 打印锁定清单（版本 / 大小 / sha256 / 用途）
python scripts/ops/fetch_static_libs.py --list
```

下载源顺序：`--mirror`（若指定）→ unpkg → jsDelivr → npmmirror（国内可直连）。
单个源内容校验不过会自动换下一个源。

## 锁定清单

| 文件 | 版本 | 大小 | 用途 | 上游（npm 包内路径） | 许可证 |
|---|---|---|---|---|---|
| `marked.min.js` | 12.0.2 | 35KB | 对话 markdown 渲染 | `marked@12.0.2/marked.min.js` | MIT |
| `mermaid.min.js` | 10.9.3 | 3.18MB | 对话里的 mermaid 图 | `mermaid@10.9.3/dist/mermaid.min.js` | MIT |
| `highlight.min.js` | 11.9.0 | 119KB | 代码块语法高亮 | `@highlightjs/cdn-assets@11.9.0/highlight.min.js` | BSD-3-Clause |
| `html-to-image.js` | 1.11.11 | 19KB | 对话 / 短线面板导出图片 | `html-to-image@1.11.11/dist/html-to-image.js` | MIT |
| `github-dark.min.css` | 11.9.0 | 1.3KB | 代码块深色主题 | `@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css` | BSD-3-Clause |

下载模板（三个源内容相同）：

- `https://unpkg.com/{pkg}@{version}/{subpath}`
- `https://cdn.jsdelivr.net/npm/{pkg}@{version}/{subpath}`
- `https://registry.npmmirror.com/{pkg}/{version}/files/{subpath}`

sha256（`--check` 即按此校验）：

```text
15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894  marked.min.js
5a8ec91820bd55afef049068489369910e5d6ce70c8103952f27e29d3e76e8bc  mermaid.min.js
837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846  highlight.min.js
0181b9a4ea3351540751b2e72b6baecb5c2297093fcb0bd2af94fc531cb0fbda  html-to-image.js
9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019  github-dark.min.css
```

## 离线 / 内网怎么补

1. 在能上网的机器上按下表下载（**文件名必须和第一列完全一致**）：

   | 文件名 | 下载地址 |
   |---|---|
   | `marked.min.js` | https://unpkg.com/marked@12.0.2/marked.min.js |
   | `mermaid.min.js` | https://unpkg.com/mermaid@10.9.3/dist/mermaid.min.js |
   | `highlight.min.js` | https://unpkg.com/@highlightjs/cdn-assets@11.9.0/highlight.min.js |
   | `html-to-image.js` | https://unpkg.com/html-to-image@1.11.11/dist/html-to-image.js |
   | `github-dark.min.css` | https://unpkg.com/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css |

2. 拷进本目录（`server/static/lib/`），不要改名、不要解压。
3. 回来校验：`python scripts/ops/fetch_static_libs.py --check`。

有内网文件服务时也可以让脚本直接拉（按「文件名」直接取，即 `<mirror>/marked.min.js`）：

```bash
python scripts/ops/fetch_static_libs.py --mirror http://<内网地址>/lib --no-cdn
```

脚本在所有源都失败时会直接打印这份「手动放置清单」，照着做即可。

## 版本是怎么锁定的

版本不是猜的，2026-11-15 用仓库内现有文件反向确认：

- `marked` / `highlight.js`：文件头 banner 直接写着版本（`marked v12.0.2`、`v11.9.0 (git: f47103d4f1)`）。
- `mermaid`：UMD 包没有 banner，取包内 version 常量 `10.9.3`，并用下载结果 sha256 与本地逐字节一致佐证。
- `html-to-image`：UMD 无版本字符串，按 sha256 反查 npm 历史版本确定为 `1.11.11`
  （`1.11.9` 与该版本字节完全相同，锁较新的 `1.11.11`）。
- `github-dark.min.css`：highlight.js 11.8.0~11.11.1 该主题字节相同，取配套的 11.9.0。
  注意仓库里备份的本地副本做过格式处理（块注释少一空行 + 末尾多一换行，CSS 语义与官方等价），
  所以它的 sha256 与官方不同；脚本把官方哈希作为基准，并把这个已知本地变体登记为 `alt_sha256`
  （`--check` 只提示不算失败，`--force` 会换成官方字节）。

升级某个库时：改 `scripts/ops/fetch_static_libs.py` 里 `LIBS` 的 `version` 与 `sha256`，
同步更新本文件的表格和哈希，然后 `--force` 重下并跑一遍页面验证。

## 其它

- 引用位置：`server/static/index.html`、`server/static/js/chat-markdown.js`、
  `server/static/js/chat-export.js`、`server/static/js/short-line.js`。
- 本文件放在 `server/static/` 而不是 `lib/`，就是为了不被 `lib/` 忽略规则误伤，能随仓库入库。
- 打包（PyInstaller）时这些静态文件需要随 `server/static` 一起带上，否则 exe 会同样缺库。
