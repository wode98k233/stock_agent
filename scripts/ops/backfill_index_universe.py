"""回补指数成分股日线到 market_data.db（因子评估的标的池数据前置）。

背景
----
因子 IC / 分层回测需要「多标的 × 同期因子值 × 未来收益」的横截面面板。
当前库中沪深 300 成分里只有 22/300 票有 ≥250 根 K 线（2026-10-16 实测），
必须先回补，否则截面 IC 只是噪声。

设计要点
--------
1. **成分快照落库**：`index_constituent` 记录 as_of_date，不做历史成分回溯
   （本项目的成分来源接口只给当下快照，硬造历史成分会引入幸存者偏差）。
2. **单票一事务**：复用 `fetch_and_store_daily`（多源降级链 + 行数校验），
   每票一次写入（约 500 行），事务短、对同时运行的服务冲击小。
3. **断点续跑**：已达标（≥ min_ratio × days）的票直接跳过；进度写 JSON，
   中断后重跑自动接着补。
4. **锁重试**：本库 `journal_mode=delete`（非 WAL），并发读写下可能
   `database is locked`——单票级别重试，不中断整批。

用法
----
    python scripts/ops/backfill_index_universe.py --dry-run --limit 3
    python scripts/ops/backfill_index_universe.py --index 000300 --days 520
    python scripts/ops/backfill_index_universe.py --codes 600519,000001

产物
----
    db/market_data.db（stock_daily / stock_info / index_constituent）
    output/backfill_<index>_progress.json（进度，供外部轮询）
    logs/backfill_<index>.log
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("backfill")


# ============================================================
# 成分拉取与落库
# ============================================================

def fetch_constituents(index_code: str) -> list[dict]:
    """拉指数成分快照。csindex 优先，失败回退新浪。"""
    import akshare as ak

    errors = []
    try:
        df = ak.index_stock_cons_csindex(symbol=index_code)
        as_of = str(df["日期"].iloc[0])[:10]
        out = []
        for _, r in df.iterrows():
            code = str(r["成分券代码"]).zfill(6)
            out.append({
                "index_code": index_code,
                "code": code,
                "name": str(r.get("成分券名称") or ""),
                "as_of_date": as_of,
                "exchange": str(r.get("交易所") or ""),
                "in_date": None,
                "source": "csindex",
                "raw_json": json.dumps(
                    {k: str(v) for k, v in r.items()}, ensure_ascii=False),
            })
        if out:
            logger.info(f"成分拉取成功（csindex）：{len(out)} 只，快照日 {as_of}")
            return out
        errors.append("csindex: 空结果")
    except Exception as e:  # noqa: BLE001
        errors.append(f"csindex: {type(e).__name__}: {e}")

    try:
        df = ak.index_stock_cons(symbol=index_code)
        as_of = datetime.now().strftime("%Y-%m-%d")
        out = []
        for _, r in df.iterrows():
            out.append({
                "index_code": index_code,
                "code": str(r["品种代码"]).zfill(6),
                "name": str(r.get("品种名称") or ""),
                "as_of_date": as_of,
                "exchange": "",
                "in_date": str(r.get("纳入日期") or "")[:10] or None,
                "source": "sina",
                "raw_json": json.dumps(
                    {k: str(v) for k, v in r.items()}, ensure_ascii=False),
            })
        if out:
            logger.info(f"成分拉取成功（sina 回退）：{len(out)} 只，快照日 {as_of}")
            return out
        errors.append("sina: 空结果")
    except Exception as e:  # noqa: BLE001
        errors.append(f"sina: {type(e).__name__}: {e}")

    raise RuntimeError("成分拉取全部失败: " + "; ".join(errors))


def upsert_constituents(rows: list[dict]) -> int:
    """写入 index_constituent（主键幂等）。

    ⚠️ 本库是 ``journal_mode=delete``（非 WAL），``make_db_context`` 的
    busy_timeout 固定 5s。若有并发的读进程（评估/服务）持有连接，写入的 commit
    会抛 "database is locked"——2026-10-17 实测整批在第一步就失败。这里做退避
    重试，把「偶发锁冲突」和「真失败」区分开。
    """
    from utils.cache.market_data_db import get_market_data_db

    if not rows:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sql = """INSERT OR REPLACE INTO index_constituent
             (index_code, code, name, as_of_date, weight, exchange, in_date,
              source, updated_at, raw_json)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    last_err = None
    for attempt in range(1, 6):
        try:
            with get_market_data_db() as conn:
                c = conn.cursor()
                for r in rows:
                    c.execute(sql, (
                        r["index_code"], r["code"], r.get("name"), r["as_of_date"],
                        r.get("weight"), r.get("exchange"), r.get("in_date"),
                        r.get("source"), now, r.get("raw_json"),
                    ))
            return len(rows)
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            logger.warning(f"成分入库遇锁，第 {attempt} 次重试：{e}")
            time.sleep(2.0 * attempt)
    raise last_err  # type: ignore[misc]


# ============================================================
# 存量检查
# ============================================================

def existing_rows(code: str) -> tuple[int, str | None, str | None]:
    """返回 (行数, 最早日期, 最晚日期)。"""
    from utils.cache.market_data_db import get_market_data_db

    with get_market_data_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) "
            "FROM stock_daily WHERE code = ?", (code,)).fetchone()
    return int(row[0] or 0), row[1], row[2]


def names_from_constituent_table(codes: list[str]) -> dict[str, str]:
    """从已入库的 index_constituent 反查名称（--codes 模式用）。"""
    from utils.cache.market_data_db import get_market_data_db

    if not codes:
        return {}
    placeholders = ",".join("?" * len(codes))
    with get_market_data_db() as conn:
        rows = conn.execute(
            f"SELECT code, name FROM index_constituent WHERE code IN ({placeholders}) "
            "ORDER BY as_of_date DESC", codes).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if r["code"] not in out and r["name"]:
            out[r["code"]] = r["name"]
    return out


def prefill_stock_info(codes: list[str], names: dict[str, str]) -> int:
    """用成分表里的名称预填 stock_info。

    ⚠️ 这是回补速度的关键一步。``_ensure_stock_info`` 在 ``stock_info.name``
    缺失或等于代码时，会调 ``_fetch_stock_name`` → ``get_stock_realtime``：
    mx_data 报「缺少必要列: 最新价」失败，再退到 sina_direct 等 20s 超时，
    最后才到 hithink —— 单票白烧 ~30s。成分表（csindex/sina）本来就带名称，
    预填后该分支直接走 ``return``（2026-10-17 实测 36.7s → 5.9s）。
    """
    from utils.cache.market_data_db import get_market_data_db, _detect_market

    # 收集需要填名称的代码（缺行 或 name 是占位符=代码）
    placeholders = ",".join("?" * len(codes)) if codes else "''"
    if not codes:
        return 0
    with get_market_data_db() as conn:
        existing = {r["code"]: r["name"] for r in conn.execute(
            f"SELECT code, name FROM stock_info WHERE code IN ({placeholders})", codes)}

    todo = [(c, (names.get(c) or "").strip()) for c in codes
            if (names.get(c) or "").strip()
            and (c not in existing or not existing[c] or existing[c] == c)]
    if not todo:
        return 0

    # ⚠️ 必须单事务批量：逐条 upsert 时每条一次 commit，在 E: 盘上 fsync 约 0.8s，
    # 500 只票光预填就要 ~7 分钟（2026-10-17 实测）。这里合并为一次事务。
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sql = """INSERT INTO stock_info (code, name, market, status, source, updated_at)
             VALUES (?, ?, ?, 'active', 'index_constituent', ?)
             ON CONFLICT(code) DO UPDATE SET
               name = CASE WHEN stock_info.name IS NULL OR stock_info.name = stock_info.code
                           THEN excluded.name ELSE stock_info.name END,
               updated_at = excluded.updated_at"""
    filled = 0
    for attempt in range(1, 6):
        try:
            with get_market_data_db() as conn:
                conn.executemany(sql, [
                    (c, n, _detect_market(c), now) for c, n in todo
                ])
            filled = len(todo)
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"stock_info 预填遇错（第 {attempt} 次）：{e}")
            time.sleep(2.0 * attempt)
    return filled


# ============================================================
# 单票回补（带锁/网络重试）
# ============================================================

# A 股年均交易日 ≈ 242；日历日/交易日 ≈ 365/242 ≈ 1.508（含长假）
_TRADING_DAYS_PER_YEAR = 242


def calendar_days_for(trading_days: int) -> int:
    """目标交易日数 → 传给抓取器的日历日数（含 60 日冗余）。"""
    return int(trading_days * 365 / _TRADING_DAYS_PER_YEAR) + 30


def backfill_one(code: str, days: int, retries: int = 3,
                 source: str = "auto") -> tuple[int, str]:
    """回补单票，返回 (写入行数, 使用的源或错误)。

    ⚠️ ``days`` 在 ``fetch_and_store_daily`` 里是**日历日**语义
    （内部 ``start = now - (days + 60)``，再 ``tail(days)``），而本脚本的
    ``--days`` 是**目标交易日数**。换算在 :func:`calendar_days_for` 完成——
    直接透传会让 520 个交易日只落 ~385 行（2026-10-17 实测）。

    指定源失败时自动回落到 ``auto``（多源降级链），避免单源盲区把整票判死。
    """
    from utils.cache.market_data_db import fetch_and_store_daily

    chain = [source] if source in ("", "auto") else [source, "auto"]
    last_err = ""
    for src in chain:
        for attempt in range(1, retries + 1):
            try:
                n = fetch_and_store_daily(code, days=days, source=src)
                if n > 0:
                    return n, src
                last_err = f"{src}: 0 行"
                break
            except sqlite3.OperationalError as e:
                last_err = f"db: {e}"
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(2.0 * attempt)
                    continue
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{src}: {type(e).__name__}: {str(e)[:160]}"
                if attempt < retries:
                    time.sleep(1.5 * attempt)
                    continue
                break
    return 0, last_err


# ============================================================
# 主流程
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="回补指数成分股日线")
    ap.add_argument("--index", default="000300", help="指数代码，默认 000300")
    ap.add_argument("--days", type=int, default=520,
                    help="每票目标交易日数（默认 520 ≈ 2 年）")
    ap.add_argument("--min-ratio", type=float, default=0.75,
                    help="存量达到 days×ratio 即跳过（默认 0.75）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    ap.add_argument("--codes", default="", help="显式指定代码，逗号分隔")
    ap.add_argument("--source", default="auto",
                    help="指定数据源（如 akshare/hithink）；auto=多源降级链")
    ap.add_argument("--sleep", type=float, default=0.15, help="每票间隔秒数")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--tag", default="",
                    help="进度/日志文件名标签；并发跑多进程时必须各自不同")
    ap.add_argument("--dry-run", action="store_true",
                    help="只拉成分并打印计划，不写库、不拉日线")
    args = ap.parse_args()

    # ⚠️ 进度/日志文件名必须每个进程独立：并发跑多进程时若共用同一路径，
    # 写 .tmp 会互相抢（PermissionError: backfill_000300_progress.tmp）。
    if args.tag:
        tag = args.tag
    elif args.codes:
        import hashlib
        tag = "codes_" + hashlib.md5(args.codes.encode()).hexdigest()[:8]
    else:
        tag = args.index

    # 日志
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"backfill_{tag}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)

    # 表结构（新增 index_constituent 依赖 ensure_schema）
    from utils.cache.market_data_db import init_market_data_tables
    init_market_data_tables()

    progress_path = ROOT / "output" / f"backfill_{tag}_progress.json"
    progress_path.parent.mkdir(exist_ok=True)

    def write_progress(**kw):
        payload = {
            "index": args.index,
            "days": args.days,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **kw,
        }
        tmp = progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(progress_path)

    # 1. 成分
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
        names = names_from_constituent_table(codes)
        as_of = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"使用显式代码 {len(codes)} 只（名称命中 {len(names)} 只）")
    else:
        t0 = time.time()
        cons = fetch_constituents(args.index)
        upsert_constituents(cons)
        codes = [r["code"] for r in cons]
        names = {r["code"]: r["name"] for r in cons}
        as_of = cons[0]["as_of_date"]
        logger.info(f"成分入库 {len(cons)} 只（{time.time()-t0:.1f}s）")

    if args.limit:
        codes = codes[: args.limit]

    target = int(args.days * args.min_ratio)
    fetch_days = calendar_days_for(args.days)
    plan, skip = [], []
    for code in codes:
        n, d1, d2 = existing_rows(code)
        (skip if n >= target else plan).append(code)

    logger.info(
        f"计划：共 {len(codes)} 只，需回补 {len(plan)} 只，"
        f"已达标跳过 {len(skip)} 只（阈值 {target} 行；"
        f"目标 {args.days} 交易日 → 抓取 {fetch_days} 日历日）")

    if args.dry_run:
        sample = plan[:10]
        for code in sample:
            n, d1, d2 = existing_rows(code)
            logger.info(f"  待补 {code} {names.get(code,'')}: 现有 {n} 行 {d1}~{d2}")
        write_progress(status="dry_run", total=len(codes), need=len(plan),
                       skip=len(skip), as_of_date=as_of)
        print(json.dumps({"total": len(codes), "need": len(plan),
                          "skip": len(skip), "sample": sample}, ensure_ascii=False))
        return 0

    # 2. 预填 stock_info（消除 _fetch_stock_name 的实时接口探测，单票省 ~30s）
    if plan:
        t0 = time.time()
        filled = prefill_stock_info(plan, names)
        logger.info(f"stock_info 预填 {filled} 只（{time.time()-t0:.1f}s）")

    # 3. 逐票回补
    done = failed = rows_added = 0
    failures: list[dict] = []
    t_start = time.time()
    write_progress(status="running", total=len(codes), need=len(plan),
                   done=0, failed=0, skip=len(skip), rows_added=0,
                   as_of_date=as_of, current="")

    for i, code in enumerate(plan, 1):
        t0 = time.time()
        n, note = backfill_one(code, fetch_days, args.retries, args.source)
        elapsed_one = time.time() - t0
        if n > 0:
            done += 1
            rows_added += n
            logger.info(f"[{i}/{len(plan)}] {code} {names.get(code,'')} "
                        f"+{n} 行（{elapsed_one:.1f}s）")
        else:
            failed += 1
            failures.append({"code": code, "name": names.get(code, ""),
                             "error": note})
            logger.warning(f"[{i}/{len(plan)}] {code} {names.get(code,'')} "
                           f"失败：{note}")

        elapsed = time.time() - t_start
        avg = elapsed / i
        write_progress(
            status="running", total=len(codes), need=len(plan),
            done=done, failed=failed, skip=len(skip), rows_added=rows_added,
            as_of_date=as_of, current=code,
            elapsed_s=round(elapsed, 1),
            eta_s=round(avg * (len(plan) - i), 1),
            failures=failures[-30:],
        )
        if args.sleep:
            time.sleep(args.sleep)

    elapsed = time.time() - t_start
    write_progress(
        status="completed", total=len(codes), need=len(plan),
        done=done, failed=failed, skip=len(skip), rows_added=rows_added,
        as_of_date=as_of, elapsed_s=round(elapsed, 1), failures=failures,
    )
    logger.info(f"完成：成功 {done}、失败 {failed}、跳过 {skip and len(skip)}、"
                f"新增 {rows_added} 行，耗时 {elapsed/60:.1f} 分钟")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
