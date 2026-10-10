"""选股雷达 - 持仓账本数据读取（portfolio_query 技能的数据层）。

只读 stock_radar.db 的 portfolio_position / portfolio_trade 快照表
（东财同步 / 手动记账 / 信号记入共用的账本），agents 层不反向依赖 server 包。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from utils.app_paths import get_db_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """打开只读连接（临时库注入供测试复用）。"""
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def get_positions(source: str = "", logger: logging.Logger | None = None,
                  db_path: str | None = None) -> dict:
    """全部持仓快照 + 账户资金 + 同步元信息。

    Returns:
        {positions: [...], account: {...}|None, meta: {synced_at, source_counts,
        note}}
        东财未同步时 account 为 None，meta.note 提示。
    """
    log = logger or logging.getLogger("radar.portfolio_data")
    conn = get_conn(db_path)
    try:
        sql = ("SELECT stock_code, stock_name, qty, avg_cost, realized_pnl, "
               "source, avail_qty, etf_flag, updated_at FROM portfolio_position"
               + (" WHERE source=?" if source else "")
               + " ORDER BY qty DESC, updated_at DESC")
        params = [source] if source else []
        rows = conn.execute(sql, params).fetchall()
        counts = {r["source"] or "manual": r["c"] for r in conn.execute(
            "SELECT source, COUNT(*) AS c FROM portfolio_position GROUP BY source")}
        synced_at = None
        if counts.get("eastmoney"):
            r = conn.execute(
                "SELECT MAX(updated_at) AS t FROM portfolio_position"
                " WHERE source='eastmoney'").fetchone()
            synced_at = r["t"]
    finally:
        conn.close()

    positions = []
    for r in rows:
        p = dict(r)
        p["is_etf"] = bool(p.pop("etf_flag", 0))
        positions.append(p)

    account = None
    try:
        from server.services.eastmoney_sync import get_account_cache
        account = get_account_cache(db_path=db_path)
    except Exception:
        account = None

    meta = {
        "synced_at": synced_at,
        "source_counts": counts,
        "note": None if account else "东财账户资金未同步（account 为 null）；持仓仅为账本快照",
    }
    log.debug(f"get_positions: {len(positions)} 只持仓, source={source or '全部'}")
    return {"positions": positions, "account": account, "meta": meta}


def get_position_detail(code: str, logger: logging.Logger | None = None,
                        db_path: str | None = None) -> dict:
    """单只持仓详情；未持仓返回 {held: False}。code 支持裸 6 位。"""
    log = logger or logging.getLogger("radar.portfolio_data")
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"无效股票代码: {code}（需 6 位数字）")
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT stock_code, stock_name, qty, avg_cost, realized_pnl, today_pnl,"
            " source, avail_qty, etf_flag, updated_at FROM portfolio_position"
            " WHERE stock_code=?", (code,)).fetchone()
    finally:
        conn.close()
    if row is None or float(row["qty"] or 0) <= 0:
        log.debug(f"get_position_detail({code}): 未持仓")
        return {"held": False, "code": code,
                "note": "当前账本中没有该股票的持仓记录"}
    p = dict(row)
    p["is_etf"] = bool(p.pop("etf_flag", 0))
    p["held"] = True
    if p["avail_qty"] is None:
        p["avail_qty_note"] = "可用数量未同步（东财同步后显示 Kysl 口径）"
    return p


def get_pnl_summary(logger: logging.Logger | None = None,
                    db_path: str | None = None) -> dict:
    """组合汇总：总市值(成本口径)/总浮盈(东财口径)/ETF 占比/盈亏 TOP3。"""
    log = logger or logging.getLogger("radar.portfolio_data")
    data = get_positions(logger=log, db_path=db_path)
    positions = [p for p in data["positions"] if float(p["qty"] or 0) > 0]

    total_cost = sum(float(p["avg_cost"] or 0) * float(p["qty"]) for p in positions)
    # 东财口径浮盈（Ckyk，含累计），非东财行无该数据 → 只统计有值的行
    em_pnls = [(p["stock_name"] or p["stock_code"], p["stock_code"],
                float(p["qty"]), float(p["avg_cost"] or 0)) for p in positions
               if p["source"] == "eastmoney"]
    etf_cost = sum(float(p["avg_cost"] or 0) * float(p["qty"])
                   for p in positions if p["is_etf"])

    # 盈亏 TOP3：现价缺失（未拉行情）时按成本市值降序；东财行若缓存有市值用之
    account = data.get("account") or {}
    top = sorted(em_pnls, key=lambda t: t[2] * t[3], reverse=True)[:3]
    top3 = [{"name": n, "code": c, "qty": q, "cost_value": round(q * cost, 2)}
            for n, c, q, cost in top]

    total_asset = account.get("total_asset")
    avail_cash = account.get("avail_cash")
    summary = {
        "position_count": len(positions),
        "total_cost": round(total_cost, 2),
        "total_market_value": account.get("positions_market_value"),
        "etf_count": len([p for p in positions if p["is_etf"]]),
        "etf_cost_ratio": round(etf_cost / total_cost, 4) if total_cost else None,
        # 账户资金（东财同步有值）：可用现金/总资产、组合当日盈亏、现金比例
        "total_asset": total_asset,
        "avail_cash": avail_cash,
        "today_pnl": account.get("today_pnl"),
        "cash_ratio": round(avail_cash / total_asset, 4)
        if avail_cash and total_asset else None,
        "source_counts": data["meta"]["source_counts"],
        "pnl_top3": top3,
        "note": "浮盈明细以东财 App 口径（Ckyk/Ljyk）为准；非东财行仅成本侧可算",
    }
    log.debug(f"get_pnl_summary: {len(positions)} 只, 总成本 {summary['total_cost']}")
    return summary


def get_trade_history(code: str = "", limit: int = 50,
                      logger: logging.Logger | None = None,
                      db_path: str | None = None) -> dict:
    """交易流水（手动/信号记账）。东财同步前历史为空——诚实标注，不编造。"""
    log = logger or logging.getLogger("radar.portfolio_data")
    limit = max(1, min(int(limit), 200))
    conn = get_conn(db_path)
    try:
        where, params = "", []
        if code:
            where = " WHERE stock_code=?"
            params.append((code or "").strip())
        rows = conn.execute(
            f"SELECT trade_uuid, stock_code, stock_name, side, qty, price,"
            f" trade_date, fee, source, remark FROM portfolio_trade{where}"
            f" ORDER BY trade_date DESC, id DESC LIMIT ?",
            params + [limit]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM portfolio_trade{where}",
            params).fetchone()["c"]
    finally:
        conn.close()
    items = [dict(r) for r in rows]
    has_any = total > 0
    meta = {
        "total": total,
        "note": (None if has_any else
                 "交易流水为空：东财同步只导入持仓快照，不含历史逐笔交易——"
                 "历史盈亏请以东财 App 显示为准，勿编造"),
    }
    log.debug(f"get_trade_history: {len(items)} 条, total={total}")
    return {"items": items, "meta": meta}
