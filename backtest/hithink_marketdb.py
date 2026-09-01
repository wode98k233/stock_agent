"""同花顺数据灌库器（P2）

将同花顺官方数据写入 market_data.db 的 stock_daily 表（source='hithink'），
供回测 / 全市场扫描复用。复用 utils.cache.market_data_db.upsert_stock_daily，
主键 (code, trade_date) 天然幂等（INSERT OR REPLACE）。

CLI 用法：
    python -m backtest.hithink_marketdb --spot                    # 全市场快照灌库（当日）
    python -m backtest.hithink_marketdb --hist 600519,000001 --start 2026-01-01

说明：同花顺 REST 无全市场历史日K批量接口（单只接口），全市场历史数据
建议用官方 marketdb（CLI 下载 Parquet + DuckDB 增量同步）离线构建；本模块
提供轻量路径：快照灌库（当日全市场）+ 指定标的的历史K灌库。
"""
import argparse
import logging
from datetime import datetime, timedelta

import pandas as pd

from utils.cache.market_data_db import upsert_stock_daily

logger = logging.getLogger("radar.hithink_marketdb")

# get_spot_em 中文列 → stock_daily 字段
_SPOT_COL_MAP = {
    "代码": "code",
    "最新价": "close",
    "涨跌额": "change_amount",
    "涨跌幅": "pct_change",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "昨收": "pre_close",
    "成交量": "volume",
    "成交额": "amount",
}

# get_stock_hist 中文列 → stock_daily 字段
_HIST_COL_MAP = {
    "日期": "trade_date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}


def _latest_trade_date() -> str:
    """最近交易日（周末回退到上周五；法定节假日边界可后续用官方交易日历接口精确化）。"""
    d = datetime.now()
    if d.weekday() == 5:    # 周六
        d = d - timedelta(days=1)
    elif d.weekday() == 6:  # 周日
        d = d - timedelta(days=2)
    return d.strftime("%Y-%m-%d")


def build_spot_records(df: pd.DataFrame, trade_date: str = None) -> list:
    """全市场快照 DataFrame（中文列）→ stock_daily records。

    快照不含名称与 trade_date（为"当下"数据），trade_date 默认取最近交易日
    （快照本质是最近一个交易日的收盘/盘中数据）。
    """
    if df is None or df.empty:
        return []
    if trade_date is None:
        trade_date = _latest_trade_date()
    records = []
    for _, row in df.iterrows():
        rec = {"trade_date": trade_date}
        ok = False
        for zh, en in _SPOT_COL_MAP.items():
            if zh in df.columns:
                val = row.get(zh)
                rec[en] = val if val is not None else None
                ok = True
        if ok and rec.get("code") is not None:
            rec["code"] = str(rec["code"]).split(".")[0]
            records.append(rec)
    return records


def build_hist_records(df: pd.DataFrame, code: str) -> list:
    """历史K线 DataFrame（中文列）→ stock_daily records。"""
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        rec = {"code": str(code).split(".")[0]}
        for zh, en in _HIST_COL_MAP.items():
            if zh in df.columns:
                rec[en] = row.get(zh)
        records.append(rec)
    return records


def sync_spot(source: str = None, trade_date: str = None) -> int:
    """全市场快照灌库（默认走 hithink 官方源），返回写入条数。"""
    if source in (None, "hithink", "auto"):
        from tools.fetcher.hithink_ds import HithinkDataSource
        df = HithinkDataSource.get_spot_em()
    else:
        from tools.fetcher import ak_spot_em
        df = ak_spot_em()
    records = build_spot_records(df, trade_date)
    if not records:
        logger.warning("快照数据为空，跳过灌库")
        return 0
    n = upsert_stock_daily(records, source="hithink")
    logger.info(f"快照灌库完成: {n} 条 (source={'hithink' if source in (None,'hithink','auto') else source})")
    return n


def sync_hist(codes: list, start: str = "", end: str = "", source: str = None) -> dict:
    """指定标的的历史K线灌库（逐只请求，失败不中断）。

    Returns:
        {"ok": 成功数, "fail": 失败数, "total": 写入总条数}
    """
    from tools.fetcher.hithink_ds import HithinkDataSource
    ok, fail, total = 0, 0, 0
    for code in codes:
        try:
            df = HithinkDataSource.get_stock_hist(code, start=start, end=end)
            records = build_hist_records(df, code)
            n = upsert_stock_daily(records, source="hithink")
            total += n
            ok += 1
            logger.info(f"历史K灌库 {code}: {n} 条")
        except NotImplementedError:
            fail += 1
            logger.warning(f"{code}: 该标的类型不支持日线灌库")
        except Exception as e:
            fail += 1
            logger.warning(f"{code}: 灌库失败 {e}")
    return {"ok": ok, "fail": fail, "total": total}


def main():
    parser = argparse.ArgumentParser(description="同花顺数据灌库器")
    parser.add_argument("--spot", action="store_true", help="全市场快照灌库（当日）")
    parser.add_argument("--hist", type=str, default="", help="历史K灌库，逗号分隔股票代码")
    parser.add_argument("--start", type=str, default="", help="历史K起始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="", help="历史K结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.spot:
        n = sync_spot()
        print(f"[OK] 快照灌库完成: {n} 条")
    if args.hist:
        codes = [c.strip() for c in args.hist.split(",") if c.strip()]
        res = sync_hist(codes, args.start, args.end)
        print(f"[OK] 历史K灌库: 成功 {res['ok']} / 失败 {res['fail']} / 写入 {res['total']} 条")
    if not args.spot and not args.hist:
        parser.print_help()


if __name__ == "__main__":
    main()
