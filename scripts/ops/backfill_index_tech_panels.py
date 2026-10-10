"""回填历史报告里被误当成个股的「指数技术面图板」（000001 → 上证指数）。

事故（2026-09-30 定位）
------------------------
`agents/analysis/report_builder.py::_maybe_build_tech_panels` 的证据回退路径从
工具证据里抓高频 6 位码，在「市场日报」类报告里必中指数码；而
`ensure_tech_panel` 只探 `stock_daily`，`000001` 因此被解析成**同码个股平安银行**
（`index_daily` 里 000001 才是上证指数），生成 `000001 / 平安银行 / 技术面图板`，
并把 `@@CHART:<id>@@` 写进报告正文。前端拿裸码取数 → 图上画的是平安银行。

代码侧已修（fragment 的 data_json 携带 `market`，市场类模板按指数解释）。
本脚本负责**存量数据**：把误判片段重建为 `market=INDEX` 版本，替换报告正文引用，
删除旧片段行。

判定口径（保守，可审计）
------------------------
对每个候选片段：
  1. 仅处理 `chart_type='tech_panel'`、`stock_code` ∈ 歧义码集合（默认由
     `index_info ∩ stock_info` 动态求出——实测只有 **000001**）、且 data_json
     尚未带 `market` 的行；
  2. 引用它的报告必须是「市场类」（命中 ≥2 个 `_MARKET_MARKERS` 标记），
     否则跳过——避免把「查平安银行」类报告里的图板误改成指数；
     `--force` 可跳过该启发式（仅在人工核对过引用清单后使用）。

幂等：已带 `market=INDEX` 的片段不再处理；重复执行输出 0 变更。

用法：
    python scripts/ops/backfill_index_tech_panels.py                  # 预演（默认，不写入）
    python scripts/ops/backfill_index_tech_panels.py --apply          # 实际写入
    python scripts/ops/backfill_index_tech_panels.py --apply --sync   # 顺带补齐指数日线
    python scripts/ops/backfill_index_tech_panels.py --codes 000001 --force
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# 允许从仓库根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agents.analysis import chart_extractor  # noqa: E402
from utils.sqlite_guard import connect as sqlite_connect  # noqa: E402

# 市场类报告标记：命中 ≥2 个才认为「该片段被引用在市場类报告里」
_MARKET_MARKERS = (
    "两市成交", "涨跌家", "A股日报", "大盘总结", "市场总结", "股市总结",
    "收盘总结", "市场温度", "午间快报", "上证指数", "深证成指", "涨跌比",
)
_MIN_MARKET_HITS = 2


def _ambiguous_codes() -> list[str]:
    """index_info ∩ stock_info：同码既是指数又是个股的码（实测只有 000001）。"""
    from utils.cache.market_data_db import get_market_data_db
    with get_market_data_db() as conn:
        idx = {r["code"] for r in conn.execute("SELECT code FROM index_info")}
        stk = {r["code"] for r in conn.execute("SELECT code FROM stock_info")}
    return sorted(idx & stk)


def _index_name(code: str) -> str:
    from utils.cache.market_data_db import get_index_name
    return str(get_index_name(code) or "")


def _market_like(text: str) -> bool:
    hits = sum(1 for m in _MARKET_MARKERS if m in (text or ""))
    return hits >= _MIN_MARKET_HITS


def _refs_of(conn: sqlite3.Connection, frag_id: str) -> list[tuple]:
    """引用该片段的消息：(id, dialog_uuid, content)。"""
    return conn.execute(
        "SELECT id, dialog_uuid, content FROM web_messages WHERE content LIKE ?",
        (f"%@@CHART:{frag_id}@@%",),
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认仅预演）")
    ap.add_argument("--sync", action="store_true", help="写入前顺带补齐指数日线")
    ap.add_argument("--force", action="store_true", help="跳过引用报告的启发式判定")
    ap.add_argument("--codes", default="", help="逗号分隔的歧义码，默认自动求交集")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or _ambiguous_codes()
    if not codes:
        print("未发现歧义码（index_info ∩ stock_info 为空），无需回填")
        return 0
    print(f"歧义码集合: {codes}")

    if args.sync:
        from utils.cache.market_data_db import fetch_and_store_index_daily
        for c in codes:
            try:
                n = fetch_and_store_index_daily(c, _index_name(c))
                print(f"[sync] 指数日线补齐 {c} {_index_name(c)}: {n} 条")
            except Exception as e:  # 补数失败不影响回填
                print(f"[sync] 指数日线补齐失败 {c}: {e}")

    db_path = chart_extractor.get_chart_db_path()
    conn = sqlite_connect(db_path, row_factory=sqlite3.Row, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        rows = conn.execute(
            "SELECT id, stock_code, stock_name, title, data_json FROM chart_fragments "
            f"WHERE chart_type='tech_panel' AND stock_code IN ({','.join('?' * len(codes))})",
            codes,
        ).fetchall()
        if not rows:
            print("没有待处理的 tech_panel 片段")
            return 0

        planned = []      # (old_id, code, old_name, refs, eligible, reason)
        for r in rows:
            try:
                meta = json.loads(r["data_json"] or "{}")
            except Exception:
                meta = {}
            if str(meta.get("market") or "").upper() == "INDEX":
                print(f"跳过（已是指数片段）: {r['stock_code']} {r['title']} id={r['id']}")
                continue
            refs = _refs_of(conn, r["id"])
            marketish = [m for m in refs if _market_like(m["content"])]
            if args.force or not refs:
                eligible, reason = True, "force/无引用"
            elif len(marketish) == len(refs):
                eligible, reason = True, f"引用报告全部为市场类({len(refs)})"
            else:
                eligible, reason = False, f"仅 {len(marketish)}/{len(refs)} 条引用为市场类"
            planned.append((r["id"], r["stock_code"], r["stock_name"], refs, eligible, reason))

        print("\n=== 处理计划 ===")
        for old_id, code, old_name, refs, eligible, reason in planned:
            flag = "回填" if eligible else "跳过"
            print(f"[{flag}] {code} {old_name} id={old_id} 引用={len(refs)} 原因={reason}")
            for m in refs:
                first = (m["content"] or "").strip().splitlines()[0][:48]
                print(f"        msg#{m['id']} {m['dialog_uuid'][:20]} | {first}")

        todo = [p for p in planned if p[4]]
        if not todo:
            print("\n没有需要回填的片段")
            return 0
        if not args.apply:
            print(f"\n（预演）将回填 {len(todo)} 个片段；加 --apply 实际写入")
            return 0

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        replaced_msgs = 0
        for old_id, code, old_name, refs, _eligible, _reason in todo:
            idx_name = _index_name(code)
            new_id = chart_extractor.ensure_tech_panel(code, name=idx_name, market="INDEX")
            if not new_id:
                print(f"!! 新建指数片段失败（本地指数日线不足？）: {code}，保留原片段")
                continue
            cur = conn.execute(
                "UPDATE web_messages SET content = replace(content, ?, ?), updated_at = ? "
                "WHERE content LIKE ?",
                (f"@@CHART:{old_id}@@", f"@@CHART:{new_id}@@", now,
                 f"%@@CHART:{old_id}@@%"),
            )
            replaced_msgs += cur.rowcount
            conn.execute("DELETE FROM chart_fragments WHERE id = ?", (old_id,))
            print(f"已回填 {code}: {old_name or '(空)'} -> {idx_name} "
                  f"(旧 {old_id[:8]} → 新 {new_id[:8]})，替换 {cur.rowcount} 条报告引用")
        conn.commit()
        print(f"\n完成：回填 {len(todo)} 个片段，替换 {replaced_msgs} 条报告引用")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
