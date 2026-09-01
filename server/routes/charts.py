"""图表数据 API

提供 K 线图、技术指标等数据接口
"""
import asyncio
import re
from functools import partial
from typing import Any, Dict, Optional
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
import pandas as pd
import numpy as np

from server.schemas import KlineSyncRequest
from server.chart_utils import round_list, ma, ema, macd, rsi, kdj, boll, dpo

router = APIRouter()


@router.post("/api/chart/kline/sync")
async def sync_kline_data_static(payload: KlineSyncRequest):
    return await sync_kline_data(payload)


# ============================================================
# Chart Fragments（agent 报告图表片段，见 docs/superpowers/specs/2026-07-06-chart-fragments-design.md）
# ============================================================

@router.get("/api/chart/{chart_id}")
async def get_chart_fragment(chart_id: str):
    """按 ID 获取图表片段（ECharts-ready JSON）。"""
    from agents.analysis.chart_extractor import get_fragment
    frag = get_fragment(chart_id)
    if not frag:
        raise HTTPException(status_code=404, detail=f"图表不存在: {chart_id}")
    return frag


@router.get("/api/charts/{dialog_uuid}")
async def list_chart_fragments(dialog_uuid: str):
    """按对话列出所有图表片段。"""
    from agents.analysis.chart_extractor import list_fragments
    items = [f for f in list_fragments(500) if f.get("dialog_uuid") == dialog_uuid]
    return {"items": items}


@router.delete("/api/chart/{chart_id}")
async def delete_chart_fragment(chart_id: str):
    """删除单个图表片段。"""
    from agents.analysis.chart_extractor import _chart_db
    with _chart_db() as conn:
        conn.execute("DELETE FROM chart_fragments WHERE id=?", (chart_id,))
    return {"ok": True}


@router.get("/api/chart/kline/{code}")
async def get_kline_data(
    code: str,
    days: int = Query(default=120, ge=1, le=1000, description="获取天数"),
    start_date: str = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end_date: str = Query(default=None, description="结束日期 YYYY-MM-DD"),
    adjust: str = Query(default="qfq", description="复权类型: qfq前复权, hfq后复权, none不复权"),
    market: str = Query(default=None, description="市场类型: INDEX 指数, 其他/空 个股"),
):
    """获取 K 线数据（含技术指标）

    Returns:
        {
            "code": "600519",
            "name": "贵州茅台",
            "kline": [
                {"date": "2024-01-02", "open": 1700, "high": 1720, "low": 1690, "close": 1710, "volume": 12345, "amount": 21000000, ...},
                ...
            ],
            "indicators": {
                "ma5": [1705, ...],
                "ma10": [1700, ...],
                "ma20": [1695, ...],
                "ma60": [1680, ...],
                "macd": {"dif": [...], "dea": [...], "hist": [...]},
                "rsi6": [...],
                "rsi12": [...],
                "rsi24": [...],
                "kdj": {"k": [...], "d": [...], "j": [...]},
                "boll": {"upper": [...], "mid": [...], "lower": [...]}
            }
        }
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, partial(_get_kline_sync, code, days, start_date, end_date, adjust, market)
    )
    if result is None:
        raise HTTPException(status_code=404, detail={
            "code": code,
            "reason": "no_local_data",
            "message": f"股票 {code} 暂无本地 K 线数据，请先同步。",
        })
    return result


def _get_kline_sync(code: str, days: int, start_date: str, end_date: str, adjust: str, market: str = None) -> dict:
    """同步获取 K 线数据（个股 + 指数）"""
    from utils.cache.market_data_db import get_stock_daily, get_stock_info, get_index_daily

    is_index = (market == 'INDEX')

    if is_index:
        # 指数：直接从 index_daily 读
        df = get_index_daily(code, start_date=start_date, end_date=end_date)
        if not df.empty and days and not start_date:
            df = df.tail(days)
    else:
        # 个股：从 stock_daily 读，无数据时 fallback 到 index_daily
        df = get_stock_daily(code, start_date=start_date, end_date=end_date, limit=days if not start_date else None)
        if df.empty:
            df = get_index_daily(code, start_date=start_date, end_date=end_date)
            if not df.empty:
                is_index = True
                if days and not start_date:
                    df = df.tail(days)

    if df.empty:
        return None

    # 获取名称
    if is_index:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            row = conn.execute('SELECT name FROM index_info WHERE code = ?', (code,)).fetchone()
            stock_name = row['name'] if row else code
    elif market == 'BOARD' or code.startswith('board_'):
        # 板块K线：从 stock_board 表获取名称
        from utils.cache.market_data_db import get_market_data_db
        board_name = code.split('_', 2)[-1] if code.startswith('board_') else code
        stock_name = board_name
    else:
        info = get_stock_info(code)
        stock_name = info['name'] if info else code

    # 复权处理（指数无复权）
    if adjust != "none" and not is_index:
        df = _apply_adjust(df, code, adjust)

    # 计算技术指标
    indicators = _calc_indicators(df)

    # 构建返回数据
    kline_data = []
    for _, row in df.iterrows():
        kline_data.append({
            'date': row['trade_date'],
            'open': round(float(row['open']), 2) if pd.notna(row['open']) else None,
            'high': round(float(row['high']), 2) if pd.notna(row['high']) else None,
            'low': round(float(row['low']), 2) if pd.notna(row['low']) else None,
            'close': round(float(row['close']), 2) if pd.notna(row['close']) else None,
            'volume': int(row['volume']) if pd.notna(row['volume']) else 0,
            'amount': round(float(row['amount']), 2) if pd.notna(row['amount']) else 0,
            'turnover_rate': round(float(row['turnover_rate']), 2) if pd.notna(row.get('turnover_rate')) else None,
            'pct_change': round(float(row['pct_change']), 2) if pd.notna(row.get('pct_change')) else None,
        })

    return {
        'code': code,
        'name': stock_name,
        'kline': kline_data,
        'indicators': indicators,
    }


# Registered above before the dynamic /api/chart/kline/{code} route.
async def sync_kline_data(payload: KlineSyncRequest):
    """显式同步单只股票 K 线数据。"""
    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    days = max(1, min(int(payload.days or 250), 1000))
    source = (payload.source or "auto").strip() or "auto"
    loop = asyncio.get_running_loop()
    try:
        count = await loop.run_in_executor(None, partial(_sync_kline_sync, code, days, source))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败: {e}") from e
    if count == 0:
        raise HTTPException(status_code=404, detail=f"未获取到 {code} 的 K 线数据")
    return {"success": True, "code": code, "count": count, "source": source}


def _sync_kline_sync(code: str, days: int, source: str = "auto") -> int:
    """在线程池中执行外部数据同步。"""
    from utils.cache.market_data_db import fetch_and_store_daily

    return fetch_and_store_daily(code, days=days, source=source)


def _apply_adjust(df: pd.DataFrame, code: str, adjust: str) -> pd.DataFrame:
    """应用复权因子"""
    from utils.cache.market_data_db import get_market_data_db

    # 获取复权因子
    with get_market_data_db() as conn:
        dates = df['trade_date'].tolist()
        placeholders = ','.join(['?'] * len(dates))
        sql = f"""SELECT trade_date, fore_adjust_factor, back_adjust_factor
                  FROM stock_adjust_factor
                  WHERE code = ? AND trade_date IN ({placeholders})
                  ORDER BY trade_date"""
        factor_df = pd.read_sql_query(sql, conn, params=[code] + dates)

    if factor_df.empty:
        return df

    # 合并复权因子
    df = df.merge(factor_df, on='trade_date', how='left')

    # 选择复权因子
    if adjust == "qfq":
        factor_col = 'fore_adjust_factor'
    else:  # hfq
        factor_col = 'back_adjust_factor'

    # 应用复权
    if factor_col in df.columns and df[factor_col].notna().any():
        latest_factor = df[factor_col].iloc[-1]
        if latest_factor and latest_factor > 0:
            for col in ['open', 'high', 'low', 'close', 'pre_close']:
                if col in df.columns:
                    df[col] = df[col] * df[factor_col] / latest_factor

    return df


def _calc_indicators(df: pd.DataFrame) -> dict:
    """计算技术指标"""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values

    indicators = {}

    # 均线 MA
    indicators['ma5'] = round_list(ma(close, 5))
    indicators['ma10'] = round_list(ma(close, 10))
    indicators['ma20'] = round_list(ma(close, 20))
    indicators['ma60'] = round_list(ma(close, 60))

    # MACD (12, 26, 9)
    dif, dea, hist = macd(close, 12, 26, 9)
    indicators['macd'] = {
        'dif': round_list(dif),
        'dea': round_list(dea),
        'hist': round_list(hist),
    }

    # RSI
    indicators['rsi6'] = round_list(rsi(close, 6))
    indicators['rsi12'] = round_list(rsi(close, 12))
    indicators['rsi24'] = round_list(rsi(close, 24))

    # KDJ (9, 3, 3)
    k, d, j = kdj(high, low, close, 9, 3, 3)
    indicators['kdj'] = {
        'k': round_list(k),
        'd': round_list(d),
        'j': round_list(j),
    }

    # BOLL (20, 2)
    upper, mid, lower = boll(close, 20, 2)
    indicators['boll'] = {
        'upper': round_list(upper),
        'mid': round_list(mid),
        'lower': round_list(lower),
    }

    # DPO (区间震荡线, 默认 20, 6)
    dpo_val, madpo_val = dpo(close, 20, 6)
    indicators['dpo'] = {
        'dpo': round_list(dpo_val),
        'madpo': round_list(madpo_val),
    }

    return indicators


@router.get("/api/chart/signals/{code}")
async def get_signals(
    code: str,
    days: int = Query(default=120, ge=1, le=1000, description="计算天数"),
):
    """获取技术指标信号（金叉死叉、布林带突破等）

    信号类型：
    - macd_golden: MACD 金叉（DIF 上穿 DEA）
    - macd_death: MACD 死叉（DIF 下穿 DEA）
    - kdj_golden: KDJ 金叉（K 上穿 D）
    - kdj_death: KDJ 死叉（K 下穿 D）
    - ma_golden: MA 金叉（MA5 上穿 MA20）
    - ma_death: MA 死叉（MA5 下穿 MA20）
    - boll_break_up: 突破布林带上轨
    - boll_break_down: 突破布林带下轨
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, partial(_calc_signals_sync, code, days)
    )
    return result


def _calc_signals_sync(code: str, days: int) -> dict:
    """同步计算技术指标信号"""
    from utils.cache.market_data_db import get_stock_daily

    df = get_stock_daily(code, limit=days)
    if df.empty:
        return {"code": code, "signals": []}

    # 计算技术指标
    indicators = _calc_indicators(df)

    # 计算信号
    signals = []

    # MACD 金叉死叉
    if indicators.get('macd') and indicators['macd'].get('dif') and indicators['macd'].get('dea'):
        dif = indicators['macd']['dif']
        dea = indicators['macd']['dea']
        for i in range(1, len(dif)):
            if dif[i] is None or dea[i] is None or dif[i-1] is None or dea[i-1] is None:
                continue
            # 金叉：DIF 从下往上穿越 DEA
            if dif[i-1] < dea[i-1] and dif[i] > dea[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "macd_golden",
                    "label": "MACD金叉",
                    "position": "below",
                })
            # 死叉：DIF 从上往下穿越 DEA
            if dif[i-1] > dea[i-1] and dif[i] < dea[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "macd_death",
                    "label": "MACD死叉",
                    "position": "above",
                })

    # KDJ 金叉死叉
    if indicators.get('kdj') and indicators['kdj'].get('k') and indicators['kdj'].get('d'):
        k = indicators['kdj']['k']
        d = indicators['kdj']['d']
        for i in range(1, len(k)):
            if k[i] is None or d[i] is None or k[i-1] is None or d[i-1] is None:
                continue
            # 金叉：K 从下往上穿越 D
            if k[i-1] < d[i-1] and k[i] > d[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "kdj_golden",
                    "label": "KDJ金叉",
                    "position": "below",
                })
            # 死叉：K 从上往下穿越 D
            if k[i-1] > d[i-1] and k[i] < d[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "kdj_death",
                    "label": "KDJ死叉",
                    "position": "above",
                })

    # MA 均线金叉死叉
    if indicators.get('ma5') and indicators.get('ma20'):
        ma5 = indicators['ma5']
        ma20 = indicators['ma20']
        for i in range(1, len(ma5)):
            if ma5[i] is None or ma20[i] is None or ma5[i-1] is None or ma20[i-1] is None:
                continue
            # 金叉：MA5 从下往上穿越 MA20
            if ma5[i-1] < ma20[i-1] and ma5[i] > ma20[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "ma_golden",
                    "label": "MA金叉",
                    "position": "below",
                })
            # 死叉：MA5 从上往下穿越 MA20
            if ma5[i-1] > ma20[i-1] and ma5[i] < ma20[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "ma_death",
                    "label": "MA死叉",
                    "position": "above",
                })

    # 布林带突破
    if indicators.get('boll') and indicators['boll'].get('upper') and indicators['boll'].get('lower'):
        upper = indicators['boll']['upper']
        lower = indicators['boll']['lower']
        for i in range(1, len(df)):
            if (upper[i] is None or lower[i] is None
                    or upper[i-1] is None or lower[i-1] is None):
                continue
            ci = df.iloc[i]['close']
            ci1 = df.iloc[i-1]['close']
            if ci is None or ci1 is None:
                continue
            # 突破上轨
            if ci > upper[i] and ci1 <= upper[i-1]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "boll_break_up",
                    "label": "突破上轨",
                    "position": "above",
                })
            # 突破下轨
            if ci < lower[i] and ci1 >= lower[i-1]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "boll_break_down",
                    "label": "突破下轨",
                    "position": "below",
                })

    # DPO 金叉死叉 + 穿越 0 轴
    if indicators.get('dpo') and indicators['dpo'].get('dpo') and indicators['dpo'].get('madpo'):
        dpo_arr = indicators['dpo']['dpo']
        madpo = indicators['dpo']['madpo']
        for i in range(1, len(dpo_arr)):
            if dpo_arr[i] is None or madpo[i] is None or dpo_arr[i-1] is None or madpo[i-1] is None:
                continue
            # 金叉：DPO 从下往上穿越 MADPO（买入信号，比 MACD/KDJ 更早触发）
            if dpo_arr[i-1] < madpo[i-1] and dpo_arr[i] > madpo[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "dpo_golden",
                    "label": "DPO金叉",
                    "position": "below",
                })
            # 死叉：DPO 从上往下穿越 MADPO（卖出信号）
            if dpo_arr[i-1] > madpo[i-1] and dpo_arr[i] < madpo[i]:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "dpo_death",
                    "label": "DPO死叉",
                    "position": "above",
                })
        # 穿越 0 轴（多空分界：DPO>0 多头 / DPO<0 空头）
        for i in range(1, len(dpo_arr)):
            if dpo_arr[i] is None or dpo_arr[i-1] is None:
                continue
            if dpo_arr[i-1] < 0 and dpo_arr[i] >= 0:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "dpo_bull",
                    "label": "DPO上穿0轴",
                    "position": "below",
                })
            if dpo_arr[i-1] > 0 and dpo_arr[i] <= 0:
                signals.append({
                    "date": df.iloc[i]['trade_date'],
                    "type": "dpo_bear",
                    "label": "DPO下穿0轴",
                    "position": "above",
                })

    return {"code": code, "signals": signals}


# ============================================================
# 策略信号扫描（K 线图集成回测）
# ============================================================

class StrategyScanRequest(BaseModel):
    code: str
    strategy_id: str
    params: Dict[str, Any] = {}
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    adjust_type: str = 'qfq'
    days: int = 120


@router.post("/api/chart/strategy-signals")
async def scan_strategy_signals(req: StrategyScanRequest):
    """扫描策略买点信号（无状态评估）

    逐 K 线独立评估策略买入/卖出条件，最新一根 bar 的布尔值即"今日是否买点"。
    返回 K 线 + 信号 + 指标，供前端 K 线图渲染。
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, partial(_scan_strategy_sync, req)
    )
    return result


def _scan_strategy_sync(req: StrategyScanRequest) -> dict:
    """同步执行策略信号扫描（线程池内调用）"""
    from backtest.signal_scanner import SignalScanner
    scanner = SignalScanner()
    return scanner.scan(
        code=req.code,
        strategy_id=req.strategy_id,
        start_date=req.start_date,
        end_date=req.end_date,
        params=req.params,
        adjust_type=req.adjust_type,
        days=req.days,
    )


@router.get("/api/chart/search")
async def search_stock(keyword: str = Query(..., min_length=1, description="股票代码或名称")):
    """搜索股票（用于 K 线图选择）

    搜索策略：
    1. 优先从 stock_info 搜索（有名称信息）
    2. 补充从 stock_daily 搜索（只有代码）
    3. 返回数据条数，帮助用户判断数据完整性
    """
    from utils.cache.market_data_db import get_market_data_db

    # 归一化：去掉前导市场前缀 sh/sz/bj（不区分大小写，可带 . 或 : 分隔），
    # 如 sh001309 / SZ.300750 / bj:830799 -> 001309 / 300750 / 830799，避免带前缀搜不到
    _norm = re.sub(r"^(sh|sz|bj)[\.:]?", "", keyword, flags=re.IGNORECASE)
    if _norm and _norm != keyword:
        keyword = _norm

    with get_market_data_db() as conn:
        c = conn.cursor()

        # 从 stock_info 搜索（有名称和市场信息）
        c.execute(
            """SELECT code, name, market, is_st
               FROM stock_info
               WHERE code LIKE ? OR name LIKE ?
               ORDER BY
                 CASE
                   WHEN code = ? THEN 0
                   WHEN code LIKE ? THEN 1
                   WHEN name LIKE ? THEN 2
                   ELSE 3
                 END,
                 code
               LIMIT 20""",
            (f"%{keyword}%", f"%{keyword}%",
             keyword, f"{keyword}%", f"%{keyword}%")
        )
        results = [dict(row) for row in c.fetchall()]

        # 从 stock_daily 补充（stock_info 中没有的）
        c.execute(
            """SELECT DISTINCT code
               FROM stock_daily
               WHERE code LIKE ?
               ORDER BY
                 CASE WHEN code = ? THEN 0 WHEN code LIKE ? THEN 1 ELSE 2 END,
                 code
               LIMIT 20""",
            (f"%{keyword}%", keyword, f"{keyword}%")
        )
        existing_codes = {r['code'] for r in results}
        for row in c.fetchall():
            code = row['code']
            if code in existing_codes:
                continue
            # 推断市场
            if code.startswith(('6', '9')):
                market = 'SH'
            elif code.startswith(('0', '3')):
                market = 'SZ'
            elif code.startswith(('4', '8')):
                market = 'BJ'
            else:
                market = 'UNKNOWN'
            results.append({
                'code': code,
                'name': '',
                'market': market,
                'is_st': 0,
            })

        # 从 index_info 搜索指数（用 code+market 组合去重，避免同代码股票和指数冲突）
        c.execute(
            """SELECT code, name FROM index_info
               WHERE code LIKE ? OR name LIKE ?
               ORDER BY
                 CASE WHEN code = ? THEN 0 WHEN code LIKE ? THEN 1 WHEN name LIKE ? THEN 2 ELSE 3 END,
                 code
               LIMIT 10""",
            (f"%{keyword}%", f"%{keyword}%",
             keyword, f"{keyword}%", f"%{keyword}%")
        )
        existing_keys = {f"{r['code']}_{r.get('market', '')}" for r in results}
        for row in c.fetchall():
            key = f"{row['code']}_INDEX"
            if key in existing_keys:
                continue
            results.append({
                'code': row['code'],
                'name': row['name'],
                'market': 'INDEX',
                'is_st': 0,
            })

        # 从 stock_board 搜索板块K线（board_industry_半导体 格式）
        c.execute(
            """SELECT board_name, board_type FROM stock_board
               WHERE board_name LIKE ?
               ORDER BY
                 CASE WHEN board_name = ? THEN 0 WHEN board_name LIKE ? THEN 1 ELSE 2 END,
                 board_name
               LIMIT 10""",
            (f"%{keyword}%", keyword, f"{keyword}%")
        )
        for row in c.fetchall():
            board_code = f"board_{row['board_type']}_{row['board_name']}"
            key = f"{board_code}_BOARD"
            if key in existing_keys:
                continue
            existing_keys.add(key)
            results.append({
                'code': board_code,
                'name': row['board_name'],
                'market': 'BOARD',
                'is_st': 0,
            })

        # 为每个结果添加数据条数
        for r in results:
            if r.get('market') == 'INDEX':
                table = 'index_daily'
            elif r.get('market') == 'BOARD':
                table = 'stock_daily'
            else:
                table = 'stock_daily'
            c.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE code = ?", (r['code'],))
            r['data_count'] = c.fetchone()['cnt']

    return {'results': results}


@router.get("/api/chart/stock-list")
async def get_stock_list(market: str = Query(default=None, description="市场 SH/SZ/BJ")):
    """获取股票列表"""
    from utils.cache.market_data_db import get_market_data_db

    with get_market_data_db() as conn:
        c = conn.cursor()
        if market:
            c.execute(
                "SELECT code, name, market FROM stock_info WHERE market = ? ORDER BY code LIMIT 100",
                (market,)
            )
        else:
            c.execute("SELECT code, name, market FROM stock_info ORDER BY code LIMIT 100")
        results = [dict(row) for row in c.fetchall()]

    return {'results': results}
