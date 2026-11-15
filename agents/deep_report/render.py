"""静态阅读页渲染（M1）：Markdown 子集 → HTML，一节一页。

不引入第三方 markdown 依赖（环境未装），只支持我们生成内容用到的语法：
标题 / 段落 / 无序列表 / 有序列表 / 表格 / 加粗 / 行内代码。
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional

from agents.deep_report.models import Outline, ReportMeta, SectionResult

_CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ea;--text:#1c2430;--muted:#667085;--brand:#176b5b;--soft:#e8f4f1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
 font:14px/1.85 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{display:grid;grid-template-columns:240px minmax(0,1fr);gap:18px;max-width:1320px;margin:0 auto;padding:20px}
.toc{position:sticky;top:18px;align-self:start;background:var(--panel);border:1px solid var(--line);
 border-radius:12px;padding:14px 16px;font-size:13px}
.toc h3{margin:0 0 8px;font-size:13px;color:var(--muted)}
.toc ol{margin:0;padding-left:18px}.toc li{margin:5px 0}
.toc a{color:var(--text);text-decoration:none}.toc a:hover{color:var(--brand)}
.page{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:28px 34px;margin-bottom:16px}
.page h1{font-size:21px;margin:0 0 6px}.page h2{font-size:17px;margin:22px 0 8px;padding-left:9px;border-left:3px solid var(--brand)}
.page p{margin:9px 0}.page ul,.page ol{margin:8px 0;padding-left:22px}
.page table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
.page th,.page td{border:1px solid var(--line);padding:6px 9px;text-align:left}
.page th{background:#fafbfc;color:var(--muted);font-weight:500}
.meta{font-size:12px;color:var(--muted)}
.verdict{background:var(--soft);border-left:3px solid var(--brand);border-radius:0 8px 8px 0;
 padding:10px 14px;margin:10px 0;font-size:13px}
.chart{width:100%;height:300px;margin:10px 0}
"""


def _inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def _table(rows: List[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[2:] if len(cells) > 1 else []
    parts = ["<table><thead><tr>"]
    parts += [f"<th>{_inline(c)}</th>" for c in head]
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def md_to_html(md: str) -> str:
    """Markdown 子集 → HTML（够读，不追求通用）。"""
    lines = (md or "").split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("|") and "|" in stripped:
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue

        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+[.)]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue

        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|\||[-*]\s|\d+[.)]\s)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def build_report_html(meta: ReportMeta, outline: Outline, sections: List[SectionResult],
                      *, charts: Optional[Dict[int, Dict[str, Dict[str, Any]]]] = None,
                      title: Optional[str] = None) -> str:
    """charts: {section_seq: {chart_id: echarts_option}} —— 图跟着章节走。"""
    title = title or outline.title or meta.title or "深度报告"
    charts = charts or {}
    toc = ["<h3>目录</h3><ol>"]
    body: List[str] = []

    summary_bits = [f"<div class='meta'>标的：{html.escape(meta.universe)} · "
                    f"模板：{html.escape(meta.template_id)} · "
                    f"字数：{meta.words if meta.words is not None else '未采集'} · "
                    f"状态：{html.escape(meta.status)}</div>"]
    if outline.thesis:
        summary_bits.append(f"<div class='verdict'>投资逻辑：{html.escape(outline.thesis)}</div>")

    for sec in sorted(sections, key=lambda s: s.seq):
        anchor = f"sec{sec.seq}"
        label = sec.title if sec.section_id == "appendix" else f"{sec.seq}. {sec.title}"
        toc.append(f"<li><a href='#{anchor}'>{html.escape(label)}</a></li>")
        content_html = md_to_html(sec.content)
        extra = ""
        for cid in (charts.get(sec.seq) or {}):
            extra += f"<div class='chart' data-chart-id='s{sec.seq}-{cid}'></div>"
        body.append(
            f"<section class='page' id='{anchor}'>"
            f"<div class='meta'>第 {sec.seq} 章 · {sec.words if sec.words is not None else '未采集'} 字"
            f"{' · 续写 ' + str(sec.continued) + ' 次' if sec.continued else ''}</div>"
            f"{content_html}{extra}</section>"
        )
    toc.append("</ol>")

    charts_json = "{}"
    if charts:
        import json
        flat = {f"s{seq}-{cid}": opt for seq, m in charts.items()
                for cid, opt in (m or {}).items()}
        charts_json = json.dumps(flat, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
<script src="/static/vendor/echarts.min.js"></script>
</head><body>
<div class="wrap">
  <aside class="toc">{''.join(toc)}</aside>
  <main>
    <section class="page">{''.join(summary_bits)}</section>
    {''.join(body)}
  </main>
</div>
<script>
const OPTIONS = {charts_json};
Object.entries(OPTIONS).forEach(([id, opt]) => {{
  const el = document.querySelector(`[data-chart-id="${{id}}"]`);
  if (el && window.echarts) {{ echarts.init(el).setOption(opt); }}
}});
</script>
</body></html>
"""


def write_html(path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
