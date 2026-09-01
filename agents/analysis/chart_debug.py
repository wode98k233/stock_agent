"""Chart Fragments Debug CLI — 独立验证图表萃取链路（不依赖 agent）

用法：
  python -m agents.analysis.chart_debug inspect  --query "药明康德 MA5 MA10 MA20 MA60"
  python -m agents.analysis.chart_debug extract  --code 603259 --query "药明康德 MA5 MA10 MA20 MA60"
  python -m agents.analysis.chart_debug preview  [--id <uuid>] [--dialog <dialog_uuid>]
  python -m agents.analysis.chart_debug list
  python -m agents.analysis.chart_debug clear

验证闭环：inspect（数据格式）→ extract（萃取正确性）→ preview（图表渲染效果）。
"""
import json
import os
import sys
import webbrowser

sys.stdout.reconfigure(encoding="utf-8")


def _load_env():
    from config import Config  # noqa: F401  触发 Config 加载 .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _resolve_stock_name(code: str) -> str:
    try:
        from utils.cache.market_data_db import get_stock_info
        info = get_stock_info(code)
        if info and info.get('name'):
            return info['name']
    except Exception:
        pass
    return code or ""


def _fetch_mx_data(query: str) -> dict:
    """真实调用 mx_data 工具核心函数，返回解析后的 JSON。"""
    from tools.other_skills.eastmoney.skills import _mx_data_query_core
    raw = _mx_data_query_core(query)
    return json.loads(raw)


def _fmt(text: str, color: str = "cyan") -> str:
    _ANSI = {"cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "dim": "\033[2m", "reset": "\033[0m"}
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def cmd_inspect(query: str):
    """真实取数并打印结构化表格 + pattern 匹配预览（校准规则用）。"""
    from agents.analysis.chart_extractor import _match_patterns
    print(_fmt(f"=== inspect: {query} ===", "cyan"))
    result = _fetch_mx_data(query)
    if result.get("status") == "failed" or result.get("error"):
        print(_fmt(f"查询失败: {result.get('error')}", "red"))
        return
    tables = result.get("tables", [])
    print(f"查询证券: {result.get('query', '')} | tables: {result.get('tables_count')} | 总行数: {result.get('total_rows')}")
    for i, table in enumerate(tables, 1):
        fieldnames = table.get("fieldnames", [])
        rows = table.get("rows", [])
        print("\n" + _fmt(f"[表{i}] {table.get('sheet_name', '')}", "green"))
        print("  fieldnames:", fieldnames)
        hits = _match_patterns(fieldnames)
        if hits:
            for pat, cols in hits:
                hit_info = f"→ 命中 [{pat['chart_type']}] {pat['title']}"
                print("  " + _fmt(hit_info, "yellow") + ": " + str(cols))
        else:
            print("  " + _fmt("→ 无 pattern 命中（列名需校准）", "red"))
        for row in rows[:2]:
            print("   ", {k: v for k, v in list(row.items())[:8]})


def cmd_extract(query: str, code: str = ""):
    """真实取数 → 规则萃取 → 打印图表 JSON → 入库。"""
    from agents.analysis.chart_extractor import extract_from_tables, insert_fragments
    print(_fmt(f"=== extract: {query} ===", "cyan"))
    result = _fetch_mx_data(query)
    if result.get("error"):
        print(_fmt(f"查询失败: {result.get('error')}", "red"))
        return
    tables = result.get("tables", [])
    name = _resolve_stock_name(code) if code else ""
    fragments = extract_from_tables(tables, stock_code=code, stock_name=name)
    if not fragments:
        print(_fmt("无图表生成（无 pattern 命中或无有效数据）", "yellow"))
        return
    for f in fragments:
        label = f"📊 [{f['chart_type']}] {f['title']}"
        print("\n" + _fmt(label, "green") + f" id={f['id'][:8]}...")
        print("  series:", json.dumps(json.loads(f["data_json"])["series"], ensure_ascii=False)[:300])
        if "markLine" in json.loads(f["data_json"]):
            print("  markLine:", json.loads(f["data_json"])["markLine"])
    inserted = insert_fragments(fragments)
    print(_fmt(f"\n已入库 {inserted}/{len(fragments)} 条（幂等去重）", "cyan"))
    print(f"查看图表: python -m agents.analysis.chart_debug preview --id {fragments[0]['id']}")


def cmd_preview(fragment_id: str = "", dialog_uuid: str = ""):
    """从 SQLite 读图表 → 生成独立 HTML → 浏览器打开。"""
    from agents.analysis.chart_extractor import get_fragment, list_fragments

    fragments = []
    if fragment_id:
        f = get_fragment(fragment_id)
        if f:
            fragments = [f]
    elif dialog_uuid:
        fragments = [f for f in list_fragments(500) if f.get("dialog_uuid") == dialog_uuid]
    else:
        fragments = list_fragments(50)
    if not fragments:
        print(_fmt("无图表数据。先运行 extract 生成图表。", "yellow"))
        return

    cards = []
    for f in fragments:
        data = f["data_json"]
        cards.append(
            f'<div class="card"><div class="card-title">{f["title"]}'
            f'<span class="card-meta">{f["chart_type"]} · {f["stock_code"]} · {f["created_at"]}</span></div>'
            f'<div class="chart" id="chart-{f["id"]}"></div></div>'
        )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Chart Fragments Preview</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  body {{ background:#f1f5f9; margin:0; padding:20px; font-family: sans-serif; }}
  .card {{ background:#fff; border-radius:10px; padding:16px 20px; margin-bottom:16px;
          box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  .card-title {{ font-weight:700; color:#1e293b; margin-bottom:10px; }}
  .card-meta {{ font-size:11px; color:#94a3b8; margin-left:10px; font-weight:400; }}
  .chart {{ width:100%; height:320px; }}
</style></head><body>
{''.join(cards)}
<script>
  const fragments = {json.dumps([{ "id": f["id"], "data": json.loads(f["data_json"]) } for f in fragments], ensure_ascii=False)};
  fragments.forEach(({ id, data }) => {{
    const el = document.getElementById('chart-' + id);
    const chart = echarts.init(el);
    const option = {{
      animation: false,
      grid: {{ left: 60, right: 30, top: 30, bottom: 40 }},
      xAxis: Object.assign({{ type: 'category' }}, data.xAxis && data.xAxis[0] || {{ data: [] }}),
      yAxis: {{ type: 'value', scale: true }},
      tooltip: {{ trigger: 'axis' }},
      series: data.series || [],
    }};
    if (data.markLine) option.series = option.series.map(s => Object.assign({{}}, s, {{ markLine: {{ data: data.markLine }} }}));
    chart.setOption(option);
    window.addEventListener('resize', () => chart.resize());
  }});
</script></body></html>"""
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart_preview.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(_fmt(f"已生成预览: {out_path}（{len(fragments)} 张图表）", "green"))
    try:
        webbrowser.open("file://" + out_path.replace("\\", "/"))
    except Exception:
        print(f"请手动打开: {out_path}")


def cmd_list():
    from agents.analysis.chart_extractor import list_fragments
    fragments = list_fragments(50)
    if not fragments:
        print("（空）")
        return
    for f in fragments:
        print(f"{f['id'][:8]}  [{f['chart_type']:<12}] {f['title']:<24} {f['stock_code']}  {f['created_at']}")


def cmd_clear():
    from agents.analysis.chart_extractor import clear_fragments
    n = clear_fragments()
    print(f"已清空 {n} 条")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    cmd = args[0] if args else "help"

    def _opt(name: str, default: str = "") -> str:
        if name in flags:
            try:
                return sys.argv[sys.argv.index(name) + 1]
            except IndexError:
                return default
        return default

    if cmd == "inspect":
        q = _opt("--query")
        if not q:
            print("用法: chart_debug inspect --query \"自然语言查询\"")
            return
        cmd_inspect(q)
    elif cmd == "extract":
        q = _opt("--query")
        if not q:
            print("用法: chart_debug extract --query \"自然语言查询\" [--code 603259]")
            return
        cmd_extract(q, code=_opt("--code"))
    elif cmd == "preview":
        cmd_preview(fragment_id=_opt("--id"), dialog_uuid=_opt("--dialog"))
    elif cmd == "list":
        cmd_list()
    elif cmd == "clear":
        cmd_clear()
    else:
        print(__doc__)


if __name__ == "__main__":
    _load_env()
    main()
