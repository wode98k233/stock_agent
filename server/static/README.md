# server/static 静态资源说明（离线资源清单）

本目录是 Web 前端的全部静态资源。**页面在无外网环境也必须能正常渲染** ——
所有第三方库一律放本地（`lib/` 与 `vendor/`），**不允许直接引用公网 CDN**。

> 为什么写得这么死：本机实测 **jsdelivr 不可达**，而图表类库的加载失败是**静默**的
> （`if (typeof echarts === 'undefined') return;`）—— 页面不报错、只是图表全空，
> 排查成本极高。历史上就发生过「全站图表都不渲染」而没人发现。

## 一、目录约定

| 目录 | 放什么 | 是否加版本号 |
|---|---|---|
| `server/static/js/` | 本项目自己写的模块（`window.StockRadar.*`） | ✅ 引用时在 `index.html` 里带 `?v=N`，每次改动**必须递增** |
| `server/static/css/` | 本项目自己的样式（颜色优先走 `variables.css`） | ✅ 同上 |
| `server/static/lib/` | 体积小、纯前端工具库（highlight / marked / mermaid / html-to-image） | ❌ 文件名固定；升级时直接替换文件 |
| `server/static/vendor/` | 体积大或曾走 CDN 的库（echarts / chart.js / jszip） | ❌ 文件名固定；升级时替换文件并同步本文件 |

## 二、离线资源清单（必须随仓库/离线包一起分发）

| 库 | 版本 | 本地路径 | 用途 | 引用处 | 官方下载 |
|---|---|---|---|---|---|
| ECharts | 5.5.0 | `vendor/echarts.min.js` | K 线、策略研究净值曲线、大屏/台账图表 | `chart-kline.js` / `chart-flow.js` / `chat-core.js` / `thesis.js` / `strategy-research.js` | https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js |
| Chart.js | 4.4.1 | `vendor/chart.umd.min.js` | 对比图（Batch/Multi Compare） | `batch-compare-chart.js` / `multi-compare-chart.js` | https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js |
| JSZip | 3.10.1 | `vendor/jszip.min.js` | 对话导出：多图打包 zip | `chat-export.js` | https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js |
| html-to-image | 1.11.x | `lib/html-to-image.js` | 对话导出为图片 | `chat-export.js` | https://cdn.jsdelivr.net/npm/html-to-image@1.11.11/dist/html-to-image.js |
| marked | — | `lib/marked.min.js` | Markdown 渲染 | `chat-markdown.js` | https://cdn.jsdelivr.net/npm/marked/marked.min.js |
| highlight.js | — | `lib/highlight.min.js` + `lib/github-dark.min.css` | 代码块高亮 | `chat-markdown.js` | https://cdn.jsdelivr.net/gh/highlightjs/cdn-release/build/highlight.min.js |
| mermaid | — | `lib/mermaid.min.js` | 流程图渲染（报告内 ```mermaid） | `chat-markdown.js` | https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js |

### 国内下载（jsdelivr 不可达时用 npmmirror）

```powershell
# 例：补齐 jszip（本仓库已随包提供，此处演示离线补包方式）
Invoke-WebRequest -Uri 'https://registry.npmmirror.com/jszip/3.10.1/files/dist/jszip.min.js' `
  -OutFile 'server/static/vendor/jszip.min.js'
# 其它库同法：https://registry.npmmirror.com/<包名>/<版本>/files/<包内路径>
```

## 三、引用规则（改前端前先看这三条）

1. **本地优先**：任何第三方库都先试本地路径；CDN 只能作为**兜底**（且必须在注释里写明为什么保留）。
   例：`chat-export.js` 的 JSZip 先 `/static/vendor/jszip.min.js`，失败才回退 CDN。
2. **自己写的 js/css 引用必须递增 `?v=N`**（`index.html` 里），否则浏览器缓存会让改动看不到。
   `lib/`、`vendor/` 的文件名固定、不带版本号（升级＝替换文件，并更新上表）。
3. **加载失败要说话**：新引入的库若加载失败必须给出可读错误（不能静默 return），
   否则用户看到的是「功能没反应」而不是「缺文件」。

## 四、离线部署自检

```powershell
# 一键检查清单里的文件是否齐全（缺哪个就补哪个）
$need = @('vendor/echarts.min.js','vendor/chart.umd.min.js','vendor/jszip.min.js',
          'lib/html-to-image.js','lib/marked.min.js','lib/highlight.min.js',
          'lib/github-dark.min.css','lib/mermaid.min.js')
$need | ForEach-Object {
  $p = "server/static/$_"
  "{0,-40} {1}" -f $_, (if (Test-Path $p) { 'OK' } else { '缺失 ✗' })
}
```

## 五、打包

PyInstaller 打包时 `server/static` 整目录随 `build_spec/main.spec` 一起进包
（含 `lib/` 与 `vendor/`），所以**离线包只需要这一份目录**；新增第三方库请确认
它落在 `lib/` 或 `vendor/` 下，并同步更新本文件第二节的表。
