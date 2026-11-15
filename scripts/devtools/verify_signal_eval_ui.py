"""AI 信号评测页验收（Playwright + 后端真实数据 fixtures）。

做什么
-----
1. 用后端真实数据生成 fixtures（`build_overview` / `_matrix_sync` / `_signals_sync` /
   `_top_sync` / `_portfolio_sync` / `_unrecognized_sync` / `build_calibration` /
   `build_conditional`），按成本档位 0 / 0.05 / 0.1 导出到 output/。
2. 起一个静态页，stub `window.StockRadar.api.fetchJSON` + `utils.esc`，加载
   `variables.css` + `signal-eval.css` + `signal-eval.js`，调用
   `window._SignalEval.open()`。
3. 断言新 4 区块、6 个周期列、排行榜、16 行矩阵、旧区块全部存在；交互
   （tabs / 成本 / 周期 / 排序 / 分页 / 详情）无 console error。
   · 配色：同一胜率值在新矩阵 / 排行榜 / 旧矩阵使用同一 tone class（≥50% 红 up、
     <50% 绿 down），旧矩阵 sell 5 日 100.0% 未被二次取反。
   · 样本表：hold 渲染为「HOLD 观望 / 无方向」，sell tab 只含 sell，配对平仓行数随 fixture。
4. 截图：output/_verify_signal_eval_full.png（1440 全页）、_top.png（1440×1200）、
   _samples.png、_2560.png（2560×1279 首屏）、_mobile.png（390）；并检查
   1440 / 2560 / 390 无横向溢出；2560 下断言版心 ≥2200px、KPI 数字 ≥34px、
   表格正文 ≥13px、表头 ≥11.5px。

运行
----
    .\\venv\\Scripts\\python.exe scripts\\devtools\\verify_signal_eval_ui.py
    .\\venv\\Scripts\\python.exe scripts\\devtools\\verify_signal_eval_ui.py --reuse-fixtures

成功：打印 ok ... 并以 0 退出；任何断言失败以非 0 退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"
FIXTURE_CACHE = OUT / "_signal_eval_fixtures.json"
JS = ROOT / "server/static/js/signal-eval.js"
CSS_DIR = ROOT / "server/static/css"
COSTS = ("0", "0.05", "0.1")

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _endpoint_files(cost: str) -> dict[str, Path]:
    suffix = "" if cost == "0" else "_c" + cost.replace(".", "")
    return {
        "overview": OUT / f"_signal_eval_overview{suffix}.json",
        "matrix": OUT / f"_signal_eval_matrix{suffix}.json",
        "signals": OUT / f"_signal_eval_signals{suffix}.json",
        "top": OUT / f"_signal_eval_top{suffix}.json",
        "portfolio": OUT / f"_signal_eval_portfolio{suffix}.json",
        "calibration": OUT / f"_signal_eval_calibration{suffix}.json",
        "conditional": OUT / f"_signal_eval_conditional{suffix}.json",
        "unrecognized": OUT / f"_signal_eval_unrecognized{suffix}.json",
    }


def build_fixtures() -> dict:
    """调用生产同步函数生成 fixtures（只读 DB，不启动服务）。"""
    from server.routes.signal_eval import (
        _matrix_sync, _portfolio_sync, _signals_sync, _top_sync, _unrecognized_sync,
    )
    from server.signal_eval_service import (
        build_calibration, build_conditional, build_overview,
    )

    OUT.mkdir(exist_ok=True)
    fixtures: dict = {"costs": {}, "unrecognized": _unrecognized_sync(1, 200)}
    for cost in COSTS:
        c = float(cost)
        data = {
            "overview": build_overview(c),
            "matrix": _matrix_sync(c),
            "signals": _signals_sync(None, 1, 200, c, False),
            "top": _top_sync(c),
            "portfolio": _portfolio_sync(c),
            "calibration": build_calibration(c),
            "conditional": build_conditional(c),
            "unrecognized": fixtures["unrecognized"],
        }
        fixtures["costs"][cost] = data
        for name, path in _endpoint_files(cost).items():
            path.write_text(json.dumps(data[name], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[fixtures] cost={cost} "
              f"signals={data['signals']['total']} symbols={len(data['overview']['symbol_stats'])} "
              f"dims={len(data['overview']['dimension_stats'])}")
    FIXTURE_CACHE.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    return fixtures


def load_fixtures() -> dict:
    return json.loads(FIXTURE_CACHE.read_text(encoding="utf-8"))


PAGE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>signal-eval verify</title></head>
<body>
<div class="bt-signal-eval-full" id="btSignalEvalView">
  <div class="bt-signal-eval-loading">加载中...</div>
</div>
</body></html>"""


def open_page(browser, fixtures: dict, width: int, height: int):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.set_content(PAGE_HTML, wait_until="load")
    page.add_style_tag(path=str(CSS_DIR / "variables.css"))
    page.add_style_tag(path=str(CSS_DIR / "signal-eval.css"))
    page.add_style_tag(content="""
      html, body { margin: 0; background: var(--panel); }
      /* 截图要整页高度；生产是弹窗内滚动容器 */
      #btSignalEvalView { height: auto !important; overflow: visible !important; }
      * { animation: none !important; transition: none !important; }
    """)
    page.evaluate("""(fixJson) => {
      const fix = JSON.parse(fixJson);
      window.__FIX = fix;
      window.__requests = [];
      window.__klineCalls = [];
      window.utils = { esc: (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])) };
      window.openKlineChart = (code, opts) => { window.__klineCalls.push({ code, opts }); };
      const route = (url) => {
        const u = new URL(url, "http://verify.local");
        const path = u.pathname.replace("/api/signal-eval/", "");
        const cost = u.searchParams.get("cost") || "0";
        return { path, cost };
      };
      window.StockRadar = { api: {
        fetchJSON: async (url, opts) => {
          window.__requests.push(url);
          const { path, cost } = route(url);
          if (path === "rescan") return { code: 0, data: { task_id: "verify-task" }, msg: "ok" };
          if (path.startsWith("rescan/")) return { code: 0, data: { pct: 1, status: "completed", added: 0 }, msg: "ok" };
          if (path === "unrecognized") return { code: 0, data: window.__FIX.unrecognized, msg: "ok" };
          const bucket = window.__FIX.costs[cost] || window.__FIX.costs["0"];
          if (!(path in bucket)) throw new Error("unknown endpoint " + path);
          return { code: 0, data: bucket[path], msg: "ok" };
        },
        portfolioFromSignal: async () => ({ code: 0, data: {}, msg: "ok" }),
      } };
    }""", json.dumps(fixtures, ensure_ascii=False))
    page.add_script_tag(path=str(JS))
    page.evaluate("() => window._SignalEval.open()")
    page.wait_for_selector("#seScorecards .se-pcard", timeout=20000)
    page.wait_for_function(
        "() => document.querySelectorAll('#seSamples tbody tr.sample-row').length > 0",
        timeout=20000)
    return page, errors


def horizontal_overflow(page) -> tuple[int, int]:
    return page.evaluate("""() => {
      const d = document.scrollingElement || document.documentElement;
      return [d.scrollWidth, window.innerWidth];
    }""")


def assert_no_overflow(page, label: str) -> None:
    sw, iw = horizontal_overflow(page)
    assert sw <= iw + 1, f"{label} 横向溢出：scrollWidth={sw} > viewport={iw}"


def check_desktop(page, fixtures: dict) -> None:
    ov = fixtures["costs"]["0"]["overview"]
    matrix = fixtures["costs"]["0"]["matrix"]
    signals = fixtures["costs"]["0"]["signals"]
    portfolio = fixtures["costs"]["0"]["portfolio"]
    cal = fixtures["costs"]["0"]["calibration"]
    top = fixtures["costs"]["0"]["top"]
    cond = fixtures["costs"]["0"]["conditional"]
    unrec = fixtures["unrecognized"]
    items = signals["items"]
    n_buy = sum(1 for s in items if s["decision"] == "buy")
    n_sell = sum(1 for s in items if s["decision"] == "sell")
    n_hold = sum(1 for s in items if s["decision"] == "hold")
    n_holding = sum(1 for s in items if s["decision"] == "buy" and not s["paired"])
    n_paired = sum(1 for s in items if s["decision"] == "buy" and s["paired"])
    n_alpha = sum(1 for s in items if s["alpha"] is not None)
    assert n_hold > 0, "fixture 缺少 hold 样本，无法验证 hold 渲染"

    # ── 新 4 区块 ──
    assert page.locator("#seOverview .se-kpi-card").count() == 4, "旧 4 KPI 未渲染"
    cards = page.locator("#seScorecards .se-pcard")
    assert cards.count() == 6, f"多周期记分卡应有 6 列，实际 {cards.count()}"
    assert page.locator("#seScorecards .se-pcard[data-period]").count() == 6
    score_txt = page.locator("#seScorecards").inner_text()
    for label in ("5", "10", "20", "30", "60", "至今"):
        assert label in score_txt, f"记分卡缺少周期 {label}"
    assert "主口径" in score_txt and "长线参考" in score_txt, "记分卡缺少主口径/长线参考标记"
    assert page.locator("#seScorecards .se-pc-ci-track").count() == 6, "记分卡 CI 细条缺失"

    lb_rows = page.locator("#seLeaderboard .se-lb-row")
    n_sym = len(ov["symbol_stats"])
    assert lb_rows.count() == min(8, n_sym), f"排行榜默认应 Top 8，实际 {lb_rows.count()}"
    lb_txt = page.locator("#seLeaderboard").inner_text()
    assert "BEST" in lb_txt and "WORST" in lb_txt, "排行榜缺少 BEST/WORST 标记"
    assert page.locator("#seLeaderboard [data-l='信号数']").count() > 0, "排行榜缺少信号数字段"
    assert page.locator("#seLeaderboard [data-l$='胜率']").count() > 0, "排行榜缺少胜率字段"
    assert page.locator("#seLeaderboard [data-l$='均值']").count() > 0, "排行榜缺少均值字段"
    for field in ("5 / 10 / 20 / 60 胜率", "最新决策", "日期", "置信度", "状态"):
        assert page.locator(f"#seLeaderboard [data-l='{field}']").count() > 0, \
            f"排行榜缺少字段 {field}"

    mx_rows = page.locator("#seMxNew tbody tr")
    n_dims = len(ov["dimension_stats"])
    assert mx_rows.count() == n_dims, f"周期×维度矩阵应 {n_dims} 行（= dimension_stats），实际 {mx_rows.count()}"
    assert n_dims >= 16, f"dimension_stats 至少 16 行（决策2/置信度4/类型2/入场2/月份6），实际 {n_dims}"
    assert page.locator("#seMxNew thead th").count() == 8, "矩阵应有 维度 + 6 周期 + 样本 = 8 列"
    mx_txt = page.locator("#seMxNew").inner_text()
    for group in ("决策", "置信度", "类型", "入场", "月份"):
        assert group in mx_txt, f"矩阵缺少分组 {group}"

    dims = page.locator("#seDims .se-dim")
    assert dims.count() == 6, f"维度切片应 6 组，实际 {dims.count()}"
    dim_txt = page.locator("#seDims").inner_text()
    for group in ("按决策", "按周期", "按置信度", "按类型", "按入场", "按月份"):
        assert group in dim_txt, f"维度切片缺少 {group}"
    assert page.locator("#seDims .se-dim-row").count() >= 16

    # ── 旧区块全部存在 ──
    assert page.locator("#sePortfolio svg").count() == 1, "组合净值 SVG 缺失"
    assert page.locator("#sePortfolio .se-eq-stat").count() == 5, "组合净值 5 项统计缺失"
    pf_txt = page.locator("#sePortfolio").inner_text()
    assert f"等权子仓位" in pf_txt and f"{portfolio['position_count']} 个" in pf_txt, \
        "组合净值缺少 position_count 等权子仓位"
    assert f"未平仓 {portfolio['open_positions']}" in pf_txt

    assert page.locator("#seMatrix tbody tr").count() == len(matrix["rows"]), "旧胜率矩阵行数不符"
    # 旧矩阵 sell 行 5 日 = 100.0%（后端已反向，前端不能再取反）
    sell_rows = page.locator("#seMatrix tbody tr").filter(has_text="sell")
    assert sell_rows.count() == 1, f"旧矩阵 sell 行应 1 行，实际 {sell_rows.count()}"
    sell_5d = sell_rows.first.locator("td").nth(1).locator(".se-wr").inner_text().strip()
    assert sell_5d == "100.0%", f"旧矩阵 sell 5 日应为 100.0%，实际 {sell_5d}"
    # 校准面板：4 个 bucket 行 + ECE 元素
    assert page.locator("#seCalibration table.se-cal tbody tr").count() == 4, \
        f"校准应有 4 个 bucket 行，实际 {page.locator('#seCalibration table.se-cal tbody tr').count()}"
    assert len(cal["buckets"]) == 4
    assert page.locator("#seCalibration .se-cal-ece").count() == 1, "校准 ECE 元素缺失"
    assert "ECE 校准总误差" in page.locator("#seCalibration .se-cal-ece").inner_text()

    # 胜率 tone 一致性：≥50% 用 up（红）、<50% 用 down（绿），三处同值同 class
    tone_rows = page.evaluate("""() => {
      const parse = (t) => {
        const m = String(t || '').match(/(\\d+(?:\\.\\d+)?)%/);
        return m ? parseFloat(m[1]) : null;
      };
      const out = [];
      const push = (root, sel, src) => {
        document.querySelectorAll(root + ' ' + sel).forEach((el) => {
          const v = parse(el.textContent);
          if (v == null) return;
          out.push({ src, value: v, cls: (el.className || '').trim() });
        });
      };
      push('#seMxNew', 'td.se-mx-c b', 'newMatrix');
      push('#seLeaderboard', '.se-lb-wr > b', 'leaderboard');
      push('#seMatrix', '.se-wr', 'oldMatrix');
      return out;
    }""")
    by_source: dict[str, int] = {}
    by_value: dict[float, set[str]] = {}
    for row in tone_rows:
        src = row["src"]
        by_source[src] = by_source.get(src, 0) + 1
        classes = row["cls"].split()
        want = "up" if row["value"] >= 50 else "down"
        assert want in classes, \
            f"{src} 胜率 {row['value']}% 的 class={row['cls']}，应为 {want}（A股口径红=高/绿=低）"
        by_value.setdefault(round(row["value"], 1), set()).update(
            c for c in classes if c in ("up", "down"))
    for src in ("newMatrix", "leaderboard", "oldMatrix"):
        assert by_source.get(src, 0) > 0, f"{src} 没有可解析的胜率单元格，配色断言失效"
    mixed = {v: cs for v, cs in by_value.items() if len(cs) > 1}
    assert not mixed, f"同一胜率值出现多种颜色语义：{mixed}"
    assert {"up", "down"} <= {c for cs in by_value.values() for c in cs}, \
        "样例里应同时出现红色（up）与绿色（down）胜率"
    assert page.locator("#seTop .se-top-col").count() == 4, "TOP5 四列缺失"
    assert page.locator("#seTop .se-top-item").count() >= 4, "TOP5 条目缺失"

    samples = page.locator("#seSamples tbody tr.sample-row")
    assert samples.count() == min(25, len(items)), \
        f"样本表默认每页 25 行，实际 {samples.count()}"
    assert page.locator("#seSamplesPag .se-pgbtn").count() > 0, "样本表分页缺失"
    assert page.locator("#seSamples thead th").count() == 15, "样本表应有 15 列"
    assert page.locator("#seBench tbody tr").count() == min(30, n_alpha), \
        f"基准 alpha 表应 {min(30, n_alpha)} 行，实际 {page.locator('#seBench tbody tr').count()}"
    assert page.locator("#seConditional tr.se-cond-row").count() == len(cond["items"]), \
        "条件单行数与 fixture 不符"
    assert page.locator("#seConditional .se-stat").count() == 6, "条件单摘要 6 项缺失"
    assert "不计入胜率" in page.locator("#seConditional").inner_text()
    assert page.locator("#seUnrecSection").is_visible(), "未识别区块应显示"
    unrec_page_rows = page.locator("#seUnrecognized tr[data-id]").count()
    assert unrec_page_rows == min(10, unrec["total"]), \
        f"未识别第 1 页应 {min(10, unrec['total'])} 行，实际 {unrec_page_rows}"
    assert page.locator("#seUnrecognized table").count() == 1
    if unrec["total"] > 10:
        assert page.locator("#seUnrecognized .se-pgbtn[data-up='next']").count() == 1

    # ── tabs 真过滤 ──
    page.click("#seSamplesPag .se-pgbtn[data-sz='all']")
    page.wait_for_function(
        f"() => document.querySelectorAll('#seSamples tbody tr.sample-row').length === {len(items)}")
    assert samples.count() == len(items), f"全部样本应 {len(items)} 行，实际 {samples.count()}"

    # ── hold 三态：HOLD 观望 / 状态=无方向（不能渲染成 sell 卖出 / 已了结） ──
    hold_rows = page.locator("#seSamples tbody tr.sample-row").filter(has_text="HOLD 观望")
    assert hold_rows.count() == n_hold, f"hold 行应 {n_hold} 条，实际 {hold_rows.count()}"
    first_hold = hold_rows.first
    hold_txt = first_hold.inner_text()
    assert "HOLD 观望" in hold_txt and "SELL 卖出" not in hold_txt, "hold 行决策 chip 渲染错误"
    hold_status = first_hold.locator("td").nth(13).inner_text().strip()
    assert hold_status != "已了结", "hold 行状态不应是已了结"
    assert hold_status == "无方向", f"hold 行状态应为无方向，实际 {hold_status}"
    for attr in ("button[data-rec]", "button[data-jump]", "button[data-detail]"):
        assert first_hold.locator(attr).count() == 1, f"hold 行缺少操作按钮 {attr}"

    page.click("#seStats .se-tab[data-f='sell']")
    page.wait_for_function("() => document.querySelectorAll('#seStats .se-tab.active').length === 1")
    assert samples.count() == n_sell, f"sell 过滤应 {n_sell} 行，实际 {samples.count()}"
    assert page.locator("#seSamples .se-chip.sell").count() == n_sell, "sell tab 混入了非 sell 行"
    assert page.locator("#seSamples .se-chip.hold").count() == 0, "sell tab 不应出现 hold 行"
    assert "HOLD 观望" not in page.locator("#seSamples").inner_text()
    assert page.locator("#seStats .se-tab.active").get_attribute("data-f") == "sell"
    assert "sell 卖出" in page.locator("#seFilterInfo").inner_text()
    assert page.locator("#seFilterReset").count() == 1, "筛选清除按钮缺失"
    page.click("#seFilterReset")
    page.wait_for_function(f"() => document.querySelectorAll('#seSamples tbody tr.sample-row').length === {len(items)}")
    assert page.locator("#seStats .se-tab.active").get_attribute("data-f") == "all"
    # buy / 持仓中 / 配对平仓 各自的动态行数（配对当前 fixture=2）
    for f, expected in (("buy", n_buy), ("holding", n_holding), ("paired", n_paired)):
        page.click(f"#seStats .se-tab[data-f='{f}']")
        page.wait_for_function(f"() => document.querySelectorAll('#seSamples tbody tr.sample-row').length === {expected}")
        assert samples.count() == expected, f"{f} 过滤应 {expected} 行，实际 {samples.count()}"
    page.click("#seFilterReset")

    # ── 样本表排序 ──
    page.click("#seSamples th.se-th-sort[data-sk='now']")
    page.wait_for_function("() => document.querySelector(\"#seSamples th.se-th-sort[data-sk='now']\").classList.contains('on')")
    th = page.locator("#seSamples th.se-th-sort[data-sk='now']")
    first_dir = th.get_attribute("aria-sort")
    assert first_dir == "descending", f"首次排序应降序，实际 {first_dir}"
    page.click("#seSamples th.se-th-sort[data-sk='now']")
    page.wait_for_function("() => document.querySelector(\"#seSamples th.se-th-sort[data-sk='now']\").getAttribute('aria-sort') === 'ascending'")
    assert page.locator("#seSamples th.se-th-sort[data-sk='now'] .se-arr").inner_text() == "↑"

    # ── 样本表分页 ──
    if len(items) > 25:
        page.click("#seSamplesPag .se-pgbtn[data-sz='25']")
        page.wait_for_function("() => document.querySelectorAll('#seSamples tbody tr.sample-row').length === 25")
        page.click("#seSamplesPag .se-pgbtn[data-p='2']")
        page.wait_for_function("() => document.querySelector('#seSamplesPag .se-pag-info').textContent.includes('第 2 /')")
        assert samples.count() == 25
        page.click("#seSamplesPag .se-pgbtn[data-sz='all']")
        page.wait_for_function(
            f"() => document.querySelectorAll('#seSamples tbody tr.sample-row').length === {len(items)}")
        assert samples.count() == len(items)

    # ── 行内详情：三通道 + 同标的 TOP ──
    page.locator("#seSamples tbody tr.sample-row").first.locator("td").nth(2).click()
    page.wait_for_selector("#seSamples .se-detail")
    assert page.locator("#seSamples .se-detail .se-chan").count() == 3, "详情三通道缺失"
    assert page.locator("#seSamples .se-detail .se-timeline").count() == 1, "详情时间线缺失"
    assert page.locator("#seSamples .se-detail .se-detail-cols").count() == 1, "同标的 TOP 缺失"
    detail_txt = page.locator("#seSamples .se-detail").inner_text()
    assert "通道①" in detail_txt and "通道②" in detail_txt and "通道③" in detail_txt
    assert "利润信号 TOP" in detail_txt and "亏损信号 TOP" in detail_txt
    page.click("#seSamples .se-detail .close-x")
    page.wait_for_function("() => document.querySelectorAll('#seSamples .se-detail').length === 0")

    # ── K 线跳转 ──
    before_k = page.evaluate("() => window.__klineCalls.length")
    page.locator("#seSamples tbody tr.sample-row").first.locator(".se-sym").click()
    assert page.evaluate("() => window.__klineCalls.length") == before_k + 1, "K 线跳转未触发"
    assert page.evaluate("() => window.__klineCalls[0].code.length") == 6

    # ── 排行榜周期 / 排序 / 展开 ──
    page.click("#seLbPeriodSeg .se-seg-btn[data-period='20d']")
    page.wait_for_function("() => document.querySelector(\"#seLbPeriodSeg .se-seg-btn[data-period='20d']\").classList.contains('active')")
    assert "20 日可评估" in page.locator("#seLbCount").inner_text()
    page.click("#seLbSortSeg .se-seg-btn[data-sort='winrate']")
    page.wait_for_function("() => document.querySelector(\"#seLbSortSeg .se-seg-btn[data-sort='winrate']\").classList.contains('active')")
    assert page.locator("#seLeaderboard .se-lb-row").count() == min(8, n_sym)
    if n_sym > 8:
        page.click("#seLbExpand")
        page.wait_for_function(f"() => document.querySelectorAll('#seLeaderboard .se-lb-row').length === {n_sym}")
        assert "收起排行榜" in page.locator("#seLbExpand").inner_text()
        page.click("#seLbExpand")
        page.wait_for_function("() => document.querySelectorAll('#seLeaderboard .se-lb-row').length === 8")
    page.click("#seLbPeriodSeg .se-seg-btn[data-period='5d']")

    # ── 未识别分页 ──
    if unrec["total"] > 10:
        page.click("#seUnrecognized .se-pgbtn[data-up='2']")
        page.wait_for_function("() => document.querySelector('#seUnrecognized .se-pag-info').textContent.includes('第 2 /')")
        assert page.locator("#seUnrecognized tr[data-id]").count() == min(10, unrec["total"] - 10)
        page.click("#seUnrecognized .se-pgbtn[data-up='1']")
        page.wait_for_function("() => document.querySelector('#seUnrecognized .se-pag-info').textContent.includes('第 1 /')")

    # ── 成本 0.05：KPI / 记分卡数字必须变化（含 cost 重拉） ──
    ov005 = fixtures["costs"]["0.05"]["overview"]
    exp005 = ov005["expectancy"]
    exp005_text = "—" if exp005 is None else (("+" if exp005 > 0 else "") + f"{exp005:.2f}%")
    avg005 = ov005["period_stats"][0]["avg"]
    avg005_text = ("+" if avg005 > 0 else "") + f"{avg005:.1f}%"
    kpi_val = "#seOverview .se-kpi-card:nth-child(2) .val"
    avg_b = "#seScorecards .se-pcard[data-period='5d'] .se-pc-meta span:nth-child(2) b"
    before = (page.locator(kpi_val).inner_text().strip(), page.locator(avg_b).inner_text().strip())
    n_req = page.evaluate("() => window.__requests.length")
    page.click("#btSignalEvalView .se-seg-btn[data-cost='0.05']")
    page.wait_for_function(
        "(expected) => document.querySelector('#seOverview .se-kpi-card:nth-child(2) .val')?.textContent.trim() === expected",
        arg=exp005_text, timeout=20000)
    after = (page.locator(kpi_val).inner_text().strip(), page.locator(avg_b).inner_text().strip())
    assert after != before, f"成本切 0.05 后 KPI/记分卡数字没有变化：{before} → {after}"
    assert page.locator(avg_b).inner_text().strip() == avg005_text, \
        f"成本 0.05 后 5 日均值应为 {avg005_text}，实际 {page.locator(avg_b).inner_text().strip()}"
    assert any("cost=0.05" in u for u in page.evaluate("() => window.__requests.slice(%d)" % n_req)), \
        "成本 0.05 切换没有带 cost 重拉"

    # ── 成本档位全页联动（overview 期望值变化 + 请求带 cost） ──
    exp01 = fixtures["costs"]["0.1"]["overview"]["expectancy"]
    exp01_text = ("+" if exp01 and exp01 > 0 else "") + f"{exp01:.2f}%" if exp01 is not None else "—"
    n_req = page.evaluate("() => window.__requests.length")
    page.click("#btSignalEvalView .se-seg-btn[data-cost='0.1']")
    page.wait_for_function(
        "(expected) => document.querySelector('#seOverview .se-kpi-card:nth-child(2) .val')?.textContent.trim() === expected",
        arg=exp01_text, timeout=20000)
    new_reqs = page.evaluate("() => window.__requests.slice(%d)" % n_req)
    assert any("cost=0.1" in u for u in new_reqs), "成本切换没有带 cost 重拉"

    # ── 重扫按钮（stub 立即完成） ──
    page.click("#seRescanBtn")
    page.wait_for_function("() => (document.querySelector('#seRescanTxt')?.textContent || '').includes('完成')", timeout=15000)
    assert page.locator("#seRescanBtn").is_enabled(), "重扫完成后按钮未恢复"


def screenshot_desktop(browser, fixtures: dict) -> None:
    page, errors = open_page(browser, fixtures, 1440, 1200)
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "_verify_signal_eval_top.png"))
    page.screenshot(path=str(OUT / "_verify_signal_eval_full.png"), full_page=True)
    page.locator("#seSamples").scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    page.screenshot(path=str(OUT / "_verify_signal_eval_samples.png"))
    assert_no_overflow(page, "1440")
    assert not errors, "console/page errors: " + " | ".join(errors[:5])
    page.close()


def check_2560(browser, fixtures: dict) -> None:
    page, errors = open_page(browser, fixtures, 2560, 1279)
    page.wait_for_timeout(300)
    assert_no_overflow(page, "2560")
    metrics = page.evaluate("""() => {
      const px = (sel) => {
        const el = document.querySelector(sel);
        return el ? parseFloat(getComputedStyle(el).fontSize) : null;
      };
      const pageEl = document.querySelector('.se-page');
      const row = document.querySelector('#seSamples tbody tr.sample-row');
      return {
        pageWidth: pageEl ? pageEl.getBoundingClientRect().width : 0,
        pageMaxWidth: pageEl ? getComputedStyle(pageEl).maxWidth : '',
        kpi: px('#seOverview .se-kpi-card .val'),
        scorecard: px('#seScorecards .se-pcard .se-pc-num'),
        tableBody: px('#seSamples tbody tr.sample-row td'),
        tableHead: px('#seSamples thead th'),
        sectionTitle: px('.se-sec-title h2'),
        rowHeight: row ? row.getBoundingClientRect().height : 0,
      };
    }""")
    for key in ("kpi", "scorecard", "tableBody", "tableHead", "sectionTitle"):
        assert metrics[key] is not None, f"2560 下找不到用于字号断言的元素：{key}"
    assert metrics["pageWidth"] >= 2200, \
        f"2560 下版心应 ≥2200px，实际 {metrics['pageWidth']:.0f}px"
    assert metrics["pageMaxWidth"] == "2240px", \
        f"2560 下 .se-page max-width 应为 2240px，实际 {metrics['pageMaxWidth']}"
    assert metrics["kpi"] >= 34, f"2560 下 KPI 数字应 ≥34px，实际 {metrics['kpi']}px"
    assert metrics["scorecard"] >= 34, \
        f"2560 下记分卡数字应 ≥34px，实际 {metrics['scorecard']}px"
    assert metrics["tableBody"] >= 13, \
        f"2560 下表格正文应 ≥13px，实际 {metrics['tableBody']}px"
    assert metrics["tableHead"] >= 11.5, \
        f"2560 下表头应 ≥11.5px，实际 {metrics['tableHead']}px"
    assert metrics["sectionTitle"] >= 18, \
        f"2560 下区块标题应 ≥18px，实际 {metrics['sectionTitle']}px"
    assert metrics["rowHeight"] >= 45, \
        f"2560 下表格行高应 ≥45px，实际 {metrics['rowHeight']}px"
    page.screenshot(path=str(OUT / "_verify_signal_eval_2560.png"))
    assert not errors, "2560 console/page errors: " + " | ".join(errors[:5])
    page.close()
    print(
        "ok 2560: page={pageWidth:.0f}px (max {pageMaxWidth}), kpi={kpi}px, "
        "score={scorecard}px, table={tableBody}px, th={tableHead}px, row={rowHeight:.0f}px".format(**metrics)
    )


def check_mobile(browser, fixtures: dict) -> None:
    page, errors = open_page(browser, fixtures, 390, 844)
    assert_no_overflow(page, "390")
    page.screenshot(path=str(OUT / "_verify_signal_eval_mobile.png"))
    # 排行榜在 390 降级为定义列表，字段必须仍在（label 来自 data-l，不依赖 CSS 伪元素）
    for label in ("信号数", "最新决策", "状态", "置信度"):
        assert page.locator(f"#seLeaderboard [data-l='{label}']").count() > 0, \
            f"移动端排行榜缺少字段 {label}"
    assert page.locator("#seLeaderboard .se-lb-row").count() == \
        min(8, len(fixtures["costs"]["0"]["overview"]["symbol_stats"]))
    page.locator("#seLeaderboard").scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "_verify_signal_eval_mobile_lb.png"))
    assert not errors, "mobile console/page errors: " + " | ".join(errors[:5])
    page.close()


def check_breakpoint(browser, fixtures: dict, width: int, height: int) -> None:
    page, errors = open_page(browser, fixtures, width, height)
    assert_no_overflow(page, f"{width}")
    total = fixtures["costs"]["0"]["signals"]["total"]
    assert page.locator("#seScorecards .se-pcard").count() == 6
    assert page.locator("#seSamples tbody tr.sample-row").count() == min(25, total)
    assert not errors, f"{width} console/page errors: " + " | ".join(errors[:5])
    page.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-fixtures", action="store_true", help="复用 output/_signal_eval_fixtures.json")
    args = ap.parse_args()

    if args.reuse_fixtures and FIXTURE_CACHE.exists():
        fixtures = load_fixtures()
        print(f"[fixtures] reuse {FIXTURE_CACHE}")
    else:
        fixtures = build_fixtures()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page, errors = open_page(browser, fixtures, 1440, 1100)
        check_desktop(page, fixtures)
        assert not errors, "console/page errors: " + " | ".join(errors[:8])
        assert_no_overflow(page, "1440")
        page.close()

        screenshot_desktop(browser, fixtures)
        check_2560(browser, fixtures)
        check_mobile(browser, fixtures)
        check_breakpoint(browser, fixtures, 1024, 900)
        check_breakpoint(browser, fixtures, 768, 900)
        browser.close()

    print("ok signal-eval UI: 4 new blocks + old blocks + interactions")
    print("screenshots:", OUT / "_verify_signal_eval_full.png", OUT / "_verify_signal_eval_top.png",
          OUT / "_verify_signal_eval_samples.png", OUT / "_verify_signal_eval_2560.png",
          OUT / "_verify_signal_eval_mobile.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
