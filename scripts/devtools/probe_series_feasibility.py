# -*- coding: utf-8 -*-
"""多期序列可行性探针（M3）：验证哪些问法能拿到「宽表序列」。

背景：`agents/deep_report/chart_candidates.py` 的 series 形状要求同一 metric 有
≥4 个**不同 period**，但取数层（fetch）目前只拿到单期快照，series 候选恒为 0。

本探针只回答一个问题：**哪些工具 + 问法能真的返回多期序列**，以及拿不到时的替代
方案（本地 `db/market_data.db` 自算）。不改任何生产代码、不写库。

用法::

    python scripts/devtools/probe_series_feasibility.py            # 全部（联网）
    python scripts/devtools/probe_series_feasibility.py --only db  # 只看本地库
    python scripts/devtools/probe_series_feasibility.py --only live # 端到端（真实工具）

输出四块：
  1. iwc_query / mx_data_query 各 3 种问法的原始形态（列名样例 / 期数 / 样例值）；
  2. 用 `agents.deep_report.series.parse_wide_series` 实解析的期数（解析器存在时）；
  3. 本地 market_data.db 的 index_daily / stock_daily 覆盖度（序列备源）；
  4. **端到端**：真实工具 → `FetchService._attach_series` → `build_candidates`
     → `compile_spec`，验证折线候选确实产出且能编译。

## 实测结论（2026-11-01，iwc_query；mx 当日额度耗尽）

| 问法 | 返回 | 能否出折线 |
|---|---|---|
| `中证银行指数(399986.SZ)近60个交易日的收盘点位` | 宽表 60 期 `收盘价[20261030]` | ✅ |
| `…近60个交易日的收盘点位和成交额` | 宽表 60 期收盘价 + 60 期成交额 | ✅（一个 payload 两组） |
| `银行板块近12个季度的净息差` | 宽表 12 期 `净息差[20260930]` | ✅ |
| `银行板块近12个季度的市净率` | 宽表 12 期 + 120 期（日频） | ✅ |
| `…近20个交易日的成交额` | **单值** `区间成交额[20260928-20261030]` | ❌ 区间合计 |
| `…近20个交易日的每日成交额` / `成交额明细` | 宽表 20 期 `成交额[20261030]` | ✅ |

**拿不到时的替代方案**（按可用性排序）：
  1. 换问法：加「每日 / 明细 / 收盘点位」，就能把「区间合计」变成逐期宽表；
  2. 本地自算：`db/market_data.db` 的 `index_daily` / `stock_daily`
     （字段 code / trade_date / close / amount / volume …，主键 (code, trade_date)）
     —— 实测 `index_daily` 有 000300 共 1098 行（2022-04-20~2026-10-30）、
     `stock_daily` 有 600036 共 683 行；**但 399986 不在库里（0 行）**，
     冷门指数要先 `fetch_and_store_index_daily()` 才有数据；
  3. 接受单期：取不到序列就只出单期卡片（出不了折线就不出图，不硬凑）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 列名里的日期：名称[20261030] / 名称[2026-10-30] / 名称(20261030)
_DATE_IN_COL = re.compile(r"[\[(]\s*(\d{4})-?(\d{2})-?(\d{2})\s*[\])]")

# ── 探针问法（同一指标，三种时长期望：日线 / 成交额 / 季度基本面） ──
IWC_QUERIES = [
    "中证银行指数(399986.SZ)近60个交易日的收盘点位",
    "中证银行指数(399986.SZ)近20个交易日的成交额",
    "银行板块近12个季度的净息差",
]

MX_QUERIES = [
    "中证银行指数(399986.SZ)近60个交易日的收盘点位",
    "中证银行板块近20个交易日的成交额",
    "银行板块近12个季度的净息差",
]


# ── 形态分析（不依赖 series.py，先独立验证「有期数可解析」） ──
def _payload_columns(obj: Any) -> List[str]:
    """尽量从各种返回形态里挖出「列名」。"""
    cols: List[str] = []
    if isinstance(obj, dict):
        raw = obj.get("columns")
        if isinstance(raw, list):
            cols.extend(str(c) for c in raw)
        # mx_data_query: tables[].fieldnames
        for t in obj.get("tables") or []:
            if isinstance(t, dict) and isinstance(t.get("fieldnames"), list):
                cols.extend(str(c) for c in t["fieldnames"])
        # 递归兜底（data.tables 之类）
        if not cols:
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    cols.extend(_payload_columns(v))
    elif isinstance(obj, list):
        for v in obj:
            cols.extend(_payload_columns(v))
    return cols


def _periods_from_columns(cols: List[str]) -> List[str]:
    """列名 → 去重日期（升序）。"""
    found: set[str] = set()
    for c in cols:
        m = _DATE_IN_COL.search(c)
        if m:
            found.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return sorted(found)


def _long_rows_periods(obj: Any) -> List[str]:
    """mx_data_query 的 tables 是「一行一日期」长表：从 rows[].date 提期数。"""
    found: set[str] = set()
    tables = obj.get("tables") if isinstance(obj, dict) else None
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        for row in t.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for key in ("date", "日期", "时间", "trade_date"):
                val = str(row.get(key) or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                    found.add(val)
    return sorted(found)


def _sample_datas(obj: Any) -> List[str]:
    """取几行列值样例（不含列名），便于人工核对单位/脏数据。"""
    out: List[str] = []
    if isinstance(obj, dict):
        datas = obj.get("datas")
        if isinstance(datas, list) and datas:
            for row in datas[:1]:
                if isinstance(row, list):
                    out.append(json.dumps(row[:4], ensure_ascii=False))
        for t in obj.get("tables") or []:
            if isinstance(t, dict):
                rows = t.get("rows") or []
                if rows:
                    out.append(json.dumps(rows[0], ensure_ascii=False)[:220])
    return out


def _iwc(query: str, limit: str = "80") -> str:
    """调 `_iwc_query_core`。

    目录名 `10jqka` 以数字开头，`import tools.other_skills.10jqka.skills` 是语法错误，
    只能 importlib（口径同 tools/skills/money_flow/main.py）。
    """
    import importlib

    mod = importlib.import_module("tools.other_skills.10jqka.skills")
    return mod._iwc_query_core(query, limit=limit)


def _mx(query: str) -> str:
    """调 `_mx_data_query_core`（返回 JSON 字符串，不清洗）。"""
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    return _mx_data_query_core(query)


def _probe(tool: str, queries: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for q in queries:
        print(f"\n── {tool} :: {q}")
        try:
            raw = _iwc(q) if tool == "iwc_query" else _mx(q)
        except Exception as e:  # noqa: BLE001 —— 探针不因单次失败中断
            print(f"   调用异常: {type(e).__name__}: {e}")
            out.append({"tool": tool, "query": q, "error": str(e)})
            continue
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001
            print(f"   非法 JSON，前 200 字: {str(raw)[:200]}")
            out.append({"tool": tool, "query": q, "error": "invalid_json"})
            continue

        status = obj.get("status")
        err = obj.get("error")
        cols = _payload_columns(obj)
        wide_periods = _periods_from_columns(cols)
        long_periods = _long_rows_periods(obj)
        periods = wide_periods or long_periods
        print(f"   status={status!r} error={err!r} 列名数={len(cols)}")
        if cols:
            print(f"   列名样例: {cols[:6]}")
        print(f"   宽表(列名日期)可解析期数={len(wide_periods)}；长表(rows.date)期数={len(long_periods)}")
        if periods:
            print(f"   期数={len(periods)} 首末={periods[0]}~{periods[-1]}")
        for s in _sample_datas(obj):
            print(f"   行样例: {s}")
        # 若解析器已实现，用生产解析器复核（这才是真正接进 fetch 的口径）
        parsed_n = None
        try:
            from agents.deep_report.series import parse_wide_series
            items = parse_wide_series(raw, "收盘价")
            parsed_n = len(items)
            print(f"   parse_wide_series 解析出 {parsed_n} 条"
                  + (f"，首末 {items[0]['period']}~{items[-1]['period']}" if items else ""))
            if items:
                print(f"   样例: {items[0]} … {items[-1]}")
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"   parse_wide_series 异常: {type(e).__name__}: {e}")
        out.append({"tool": tool, "query": q, "status": status, "error": err,
                    "n_columns": len(cols), "wide_periods": len(wide_periods),
                    "long_periods": len(long_periods), "parsed_items": parsed_n})
    return out


# ── 本地备源：market_data.db 的 index_daily / stock_daily ──
def _probe_db() -> List[Dict[str, Any]]:
    from config import Config

    path = Path(Config.get_market_data_db_path())
    print(f"\n── 本地备源 market_data.db: {path}")
    if not path.exists():
        print("   库文件不存在")
        return []
    rows_out: List[Dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, codes in (("index_daily", ["399986", "000300"]),
                             ("stock_daily", ["600036", "000001"])):
            for code in codes:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM {table} WHERE code = ?",
                        (code,)).fetchone()
                except sqlite3.Error as e:
                    print(f"   {table}/{code}: 查询失败 {e}")
                    continue
                n, lo, hi = (row or (0, None, None))
                print(f"   {table:<12} {code:<8} 行数={n:<7} 区间={lo}~{hi}")
                rows_out.append({"table": table, "code": code, "rows": n,
                                 "start": lo, "end": hi})
        # 指数维表名称（确认 399986 是「中证银行」而非同码个股）
        for code in ("399986", "000300"):
            try:
                row = conn.execute("SELECT name FROM index_info WHERE code = ?", (code,)).fetchone()
                print(f"   index_info[{code}] = {row[0] if row else None!r}")
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return rows_out


# ── 端到端：真实工具 → FetchService 补序列 → 折线候选 → 编译器 ──
def _probe_live_card(universe: str = "中证银行指数(399986.SZ)") -> List[Dict[str, Any]]:
    """验证「折线候选是否真的产出」：走生产同一批工具与同一套函数。

    只调 `FetchService._attach_series`（子代理那一跳要 LLM，探针里跳过）；
    取数成功与否、期数、候选形状、能否编译，全部打印。
    """
    from agents.deep_report.chart_candidates import build_candidates
    from agents.deep_report.chart_compiler import ChartSpecError, compile_spec
    from agents.deep_report.fetch import FetchService
    from agents.deep_report.tools_provider import load_tools

    out: List[Dict[str, Any]] = []
    print(f"\n── 端到端（真实工具）universe={universe}")
    tools = load_tools(whitelist=("iwc_query", "mx_data_query"))
    print(f"   装载工具: {sorted(tools)}")
    if not tools:
        print("   ⚠️ 工具装载为空，跳过（环境缺依赖时属正常）")
        return out

    class _EchoExecutor:
        pass

    ex = _EchoExecutor()
    ex.tools = tools
    for slot in ("snapshot", "valuation"):
        svc = FetchService(executor=ex, store=None, rid="probe")
        seed = [{"metric": "最新价", "value": 7962.26, "unit": "点",
                 "source": "iwc_query", "period": "2026-10-30"}]
        items, charts = svc._attach_series(slot, universe, "iwc_query", seed, [],
                                           ev_id="ev_probe", asof="2026-10-30")
        n_series = len([it for it in items if it.get("period") and it["metric"] != "最新价"])
        print(f"   [{slot}] 序列点={n_series} charts={[c.get('type') for c in charts]}")
        card = {"ev_id": "ev_probe", "slot": slot, "status": "ok", "asof": "2026-10-30",
                "items": items, "charts": charts, "gaps": []}
        cands = build_candidates({slot: card})
        shapes = [f"{c['shape']}({c['size']})" for c in cands]
        print(f"   [{slot}] 候选: {shapes or '无'}")
        line = next((c for c in cands if c["shape"] == "series"), None)
        if line:
            try:
                opt = compile_spec(line["spec"])
                print(f"   [{slot}] 折线编译 OK: x={len(opt['xAxis']['data'])} 点，"
                      f"series={[s['name'] for s in opt['series']]}")
                out.append({"slot": slot, "series_points": line["size"],
                            "compiled": True, "title": line["title"]})
            except ChartSpecError as e:
                print(f"   [{slot}] ❌ 折线编译失败: {e}")
                out.append({"slot": slot, "series_points": line["size"],
                            "compiled": False, "error": str(e)})
        else:
            out.append({"slot": slot, "series_points": 0, "compiled": False})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="多期序列可行性探针")
    ap.add_argument("--only", choices=["iwc", "mx", "db", "live"],
                    help="只跑指定一块（live=真实工具→卡片→折线候选的端到端验证）")
    ap.add_argument("--universe", default="中证银行指数(399986.SZ)", help="端到端用的标的")
    args = ap.parse_args()

    print("=" * 78)
    print("多期序列可行性探针（联网；本地库只读）")
    print("=" * 78)

    summary: Dict[str, Any] = {}
    if args.only in (None, "iwc"):
        summary["iwc_query"] = _probe("iwc_query", IWC_QUERIES)
    if args.only in (None, "mx"):
        summary["mx_data_query"] = _probe("mx_data_query", MX_QUERIES)
    if args.only in (None, "db"):
        summary["db"] = _probe_db()
    if args.only in (None, "live"):
        summary["live"] = _probe_live_card(args.universe)

    print("\n" + "=" * 78)
    print("小结")
    print("=" * 78)
    for tool in ("iwc_query", "mx_data_query"):
        for r in summary.get(tool) or []:
            if r.get("error"):
                print(f"  {tool:<14} 失败({r['error']}) :: {r['query']}")
            else:
                print(f"  {tool:<14} 宽表期数={r['wide_periods']:<4}"
                      f" 长表期数={r['long_periods']:<4} :: {r['query']}")
    for r in summary.get("live") or []:
        flag = "折线候选 OK" if r.get("compiled") else "无折线候选"
        print(f"  live          [{r['slot']}] {flag} 期数={r['series_points']} "
              f"{r.get('title') or r.get('error') or ''}")
    for r in summary.get("db") or []:
        print(f"  {r['table']:<12} {r['code']:<8} 行数={r['rows']:<7} {r['start']}~{r['end']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
