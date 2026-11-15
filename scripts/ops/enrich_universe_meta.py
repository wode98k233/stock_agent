"""补齐中性化所需的元数据：成分权重（市值代理）+ 行业分类。

为什么需要
----------
因子评估里的「行业/市值中性化」（PRD F9）需要两个回归变量：
- **行业**：``stock_board`` / ``stock_board_member`` 里已有（industry 类型）
- **市值**：库里 ``stock_info.float_share`` 全为 0；eastmoney 市值接口被网络拦截
  （2026-10-17 实测 ``stock_zh_a_spot_em`` 报 ProxyError/RemoteDisconnected）

替代方案：用 **csindex 成分权重**作市值代理。CSI 指数按自由流通市值加权，
权重 ∝ 自由流通市值；中性化回归只用 ``log(size)`` 作解释变量，整体乘一个常数
不改变残差（只会平移截距），因此权重在「同一指数池内」是数学上等价的大小代理。
局限：指数对超大权重有上限（capping），会轻微压扁头部；权重是快照，不随时间变化。
这两点写进报告的 caveats。

行业去重
--------
两套行业体系并存（akshare 178 只 / sina 68 只），同一代码可能落在多个板块。
取「**覆盖该票的成员数最少的板块**」= 最具体的分类；同分时 akshare 优先。

用法
----
    python scripts/ops/enrich_universe_meta.py --weights 000300,000905
    python scripts/ops/enrich_universe_meta.py --industry
    python scripts/ops/enrich_universe_meta.py --all
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sync_weights(index_codes: list[str]) -> dict:
    """拉 csindex 成分权重并写回 index_constituent。"""
    import akshare as ak
    from utils.cache.market_data_db import get_market_data_db, init_market_data_tables

    init_market_data_tables()
    report = {}
    for idx in index_codes:
        try:
            df = ak.index_stock_cons_weight_csindex(symbol=idx)
        except Exception as e:  # noqa: BLE001
            report[idx] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if df is None or df.empty:
            report[idx] = {"error": "空结果"}
            continue
        wdate = str(df["日期"].iloc[0])[:10]
        rows = [(float(r["权重"]), wdate, str(r["成分券代码"]).zfill(6), idx)
                for _, r in df.iterrows()]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_market_data_db() as conn:
            cur = conn.cursor()
            cur.executemany(
                "UPDATE index_constituent SET weight=?, weight_date=?, updated_at=? "
                "WHERE index_code=? AND code=?",
                [(w, wd, now, ic, code) for w, wd, code, ic in rows])
            updated = cur.rowcount
        report[idx] = {"weight_date": wdate, "rows": len(rows), "updated": updated}
    return report


def build_industry_map() -> dict[str, tuple[str, str]]:
    """code → (industry, source)。取最具体的板块（成员数最少）。"""
    from utils.cache.market_data_db import get_market_data_db

    sql = """
        SELECT m.code, b.board_name, b.provider,
               (SELECT COUNT(*) FROM stock_board_member x WHERE x.board_id = b.board_id) AS size
        FROM stock_board_member m
        JOIN stock_board b ON b.board_id = m.board_id
        WHERE b.board_type = 'industry'
    """
    best: dict[str, tuple[int, int, str, str]] = {}
    with get_market_data_db() as conn:
        rows = conn.execute(sql).fetchall()
    for code, name, provider, size in rows:
        if not name:
            continue
        # 排序键：akshare 优先(0) → 板块成员数最少 → 板名稳定排序
        rank = (0 if provider == "akshare" else 1, int(size or 9999), str(name))
        cur = best.get(str(code))
        if cur is None or rank < cur[:3]:
            best[str(code)] = (rank[0], rank[1], rank[2], provider)
    return {code: (v[2], v[3]) for code, v in best.items()}


def sync_industry() -> dict:
    """把行业写入 stock_info.industry（不存在则跳过并计数）。"""
    from utils.cache.market_data_db import get_market_data_db, init_market_data_tables

    init_market_data_tables()
    mapping = build_industry_map()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated = 0
    with get_market_data_db() as conn:
        cur = conn.cursor()
        for code, (name, source) in mapping.items():
            cur.execute(
                "UPDATE stock_info SET industry=?, industry_source=?, updated_at=? "
                "WHERE code=?", (name, source, now, code))
            updated += cur.rowcount
    return {"mapped": len(mapping), "updated": updated}


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐权重与行业元数据")
    ap.add_argument("--weights", default="", help="逗号分隔的指数代码")
    ap.add_argument("--industry", action="store_true")
    ap.add_argument("--all", action="store_true", help="= --weights 000300,000905 --industry")
    args = ap.parse_args()

    if args.all:
        args.weights = "000300,000905"
        args.industry = True

    out = {}
    if args.weights:
        idxs = [x.strip() for x in args.weights.split(",") if x.strip()]
        out["weights"] = sync_weights(idxs)
        print(json.dumps(out["weights"], ensure_ascii=False, indent=2))
    if args.industry:
        out["industry"] = sync_industry()
        print(json.dumps(out["industry"], ensure_ascii=False, indent=2))
    if not out:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
