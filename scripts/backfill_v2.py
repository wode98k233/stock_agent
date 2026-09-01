#!/usr/bin/env python
"""
记忆系统 v2 回填脚本 — 扫描历史语义记忆，按来源回填 confidence + trace_run_id。

用法:
    python scripts/backfill_v2.py [--dry-run] [--limit 100]

逻辑:
  1. 扫描 semantic_memory_meta 中 confidence=0.5 的记录（v1 默认值 = 未计算）
  2. 按 entry 内容判断来源类型 → 基础置信度
  3. 从日期计算新鲜度衰减
  4. 从 episode_log 按日期范围关联 trace_run_id
  5. UPDATE 回写

注意:
  - 无法精确关联 trace_run_id 的记录填 ""
  - 已迁移过的记录（confidence != 0.5）跳过
  - 使用 ConfidenceCalculator 统一计算
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def get_db_path():
    from utils.app_paths import get_stock_memory_db_path
    return get_stock_memory_db_path()


def infer_source(pe, roe, tags_json, source_query):
    """根据已有字段反推来源类型。"""
    # 有硬数据 → tool_hard_data
    if pe is not None or roe is not None:
        return "tool_hard_data"
    # 有 tags（仪表盘产出）→ dashboard_llm
    if tags_json:
        try:
            tags = json.loads(tags_json)
            if isinstance(tags, list) and len(tags) > 0:
                return "dashboard_llm"
        except json.JSONDecodeError:
            pass
    # 有 source_query → report_fallback
    if source_query:
        return "report_fallback"
    return "llm_speculation"


def compute_backfill_confidence(date_str, source):
    """计算回填置信度（不含 feedback 和 corroboration——历史数据无此信息）。

    Returns:
        (confidence: float, factors: dict)
    """
    from memory.confidence import ConfidenceCalculator
    calc = ConfidenceCalculator()
    base = calc.compute_base(source)
    freshness = calc.compute_freshness(date_str) if date_str else 0.7
    # feedback/corroboration 历史无数据，使用默认 1.0
    conf = base * freshness * 1.0 * 1.0
    conf = max(0.0, min(1.0, round(conf, 4)))
    factors = {
        "source": source,
        "base": base,
        "freshness": round(freshness, 3),
        "feedback": 1.0,
        "corroboration": 1.0,
        "_backfilled": True,
    }
    return conf, factors


def find_trace_run_id(conn, entry_date, stock_code):
    """按日期（±2 天）和股票代码从 episode_log 查找 trace_run_id。"""
    if not entry_date:
        return ""
    # 尝试精确匹配
    row = conn.execute(
        "SELECT trace_run_id FROM episode_log "
        "WHERE stocks_mentioned LIKE ? AND date BETWEEN ? AND ? "
        "ORDER BY date DESC LIMIT 1",
        (f"%{stock_code}%", entry_date[:10],
         (datetime.strptime(entry_date[:10], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
    ).fetchone()
    if row and row[0]:
        return row[0]
    return ""


def backfill(dry_run: bool = True, limit: int = 0):
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(f"[backfill] DB 不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 确认列存在
    cols = [r[1] for r in conn.execute("PRAGMA table_info(semantic_memory_meta)").fetchall()]
    if "confidence" not in cols:
        print("[backfill] semantic_memory_meta 尚无 confidence 列，请先启动一次服务让 migration 执行")
        conn.close()
        return

    # 扫描待回填记录
    sql = "SELECT * FROM semantic_memory_meta WHERE confidence = 0.5"
    if limit > 0:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql).fetchall()

    total = len(rows)
    updated = 0
    skipped = 0
    print(f"[backfill] 扫描到 {total} 条待回填记录（confidence=0.5）{' [DRY RUN]' if dry_run else ''}")

    for r in rows:
        entry_id = r["entry_id"]
        date_str = r["date"] or ""
        pe = r["pe"]
        roe = r["roe"]
        tags_json = r["tags"]
        source_query = r["source_query"] or ""
        stock_code = r["stock_code"] or ""

        # 1. 来源推断
        source = infer_source(pe, roe, tags_json, source_query)

        # 2. 置信度
        confidence, factors_dict = compute_backfill_confidence(date_str, source)
        factors = json.dumps(factors_dict, ensure_ascii=False)

        # 3. trace_run_id 关联
        trace_run_id = find_trace_run_id(conn, date_str, stock_code)
        provenance = json.dumps({
            "trace_run_id": trace_run_id,
            "_backfilled": True,
        }, ensure_ascii=False)

        # 4. M2: 时效性分类
        memory_category = "general"
        ttl_days = 30
        expires_at_val = None
        if "memory_category" in cols:
            try:
                tags = json.loads(tags_json) if tags_json else []
            except json.JSONDecodeError:
                tags = []
            content = r["raw_content"] if "raw_content" in r.keys() else (source_query or "")
            # 简单分类：tags > content > extracts
            from memory.freshness import FreshnessManager
            fm = FreshnessManager()
            # 构造临时 entry 用于 classify
            class TempEntry:
                pass
            te = TempEntry()
            te.metadata = {"tags": tags}
            te.content = content or ""
            memory_category = fm.classify(entry=te, extracts=[{"pe": pe, "roe": roe}] if (pe or roe) else None)
            ttl_days = fm.compute_ttl(memory_category)
            expires_at_val = fm.compute_expires_at(date_str, ttl_days) if date_str else None

        if not dry_run:
            # M1 + M2 update
            conn.execute(
                "UPDATE semantic_memory_meta SET confidence=?, confidence_factors=?, "
                "provenance=?, trace_run_id=?, "
                "memory_category=?, ttl_days=?, expires_at=? "
                "WHERE entry_id=?",
                (confidence, factors, provenance, trace_run_id,
                 memory_category, ttl_days, expires_at_val,
                 entry_id)
            )
            updated += 1
            if updated % 50 == 0:
                conn.commit()
                print(f"  ... {updated}/{total}")

        if dry_run and updated == 0:
            print(f"  [{entry_id[:12]}] {stock_code} date={date_str[:10]} "
                  f"source={source} cat={memory_category} ttl={ttl_days}d conf={confidence} "
                  f"trace={trace_run_id[:12] if trace_run_id else 'N/A'}")

    if not dry_run:
        conn.commit()

    # ── M3 pass: 按时序标记 superseded ──
    if not dry_run and "superseded_by" in cols:
        print("[backfill] M3 pass: 标记 superseded 链...")
        stocks = conn.execute(
            "SELECT DISTINCT stock_code FROM semantic_memory_meta WHERE stock_code != ''"
        ).fetchall()
        m3_count = 0
        for (sc,) in stocks:
            rows_s = conn.execute(
                "SELECT entry_id, sentiment, date FROM semantic_memory_meta "
                "WHERE stock_code=? AND superseded_by='' "
                "ORDER BY date DESC",
                (sc,)
            ).fetchall()
            if len(rows_s) < 2:
                continue
            # 时间倒序：最新的 keep，older ones with different sentiment → superseded
            newest = rows_s[0]
            for older in rows_s[1:]:
                if older["sentiment"] and newest["sentiment"] and older["sentiment"] != newest["sentiment"]:
                    conn.execute(
                        "UPDATE semantic_memory_meta SET superseded_by=?, "
                        "superseded_at=datetime('now'), supersede_reason='new_analysis' "
                        "WHERE entry_id=?",
                        (newest["entry_id"], older["entry_id"])
                    )
                    m3_count += 1
        conn.commit()
        print(f"[backfill] M3: {m3_count} 条标记为 superseded")

    if dry_run:
        print(f"[backfill] DRY RUN 完成，将回填 {total} 条记录（加 --no-dry-run 执行）")
    else:
        print(f"[backfill] 完成 — 已更新 {updated} 条, 跳过 {skipped} 条")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="记忆系统 v2 回填")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="预览模式（默认）")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="实际执行回填")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制回填条数（0=全部）")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
