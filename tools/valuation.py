import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, Any

logger = logging.getLogger("radar.valuation")


PE_THRESHOLDS = [
    (0, 10, '极度低估'),
    (10, 20, '低估'),
    (20, 40, '合理'),
    (40, 70, '偏高'),
    (70, float('inf'), '高估'),
]

PB_THRESHOLDS = [
    (0, 0.5, '极度低估'),
    (0.5, 1.0, '低估'),
    (1.0, 3.0, '合理'),
    (3.0, 6.0, '偏高'),
    (6.0, float('inf'), '高估'),
]

PS_THRESHOLDS = [
    (0, 2, '低估'),
    (2, 5, '合理'),
    (5, 10, '偏高'),
    (10, float('inf'), '高估'),
]

PEG_THRESHOLDS = [
    (0, 0.5, '极度低估'),
    (0.5, 1.0, '低估'),
    (1.0, 1.5, '合理'),
    (1.5, 2.0, '偏高'),
    (2.0, float('inf'), '高估'),
]

DIVIDEND_YIELD_THRESHOLDS = [
    (0, 1, '偏低'),
    (1, 3, '合理'),
    (3, 5, '较高'),
    (5, float('inf'), '优秀'),
]

PERCENTILE_LEVELS = [
    (0, 10, '极度低估'),
    (10, 30, '低估'),
    (30, 70, '合理'),
    (70, 90, '偏高'),
    (90, 101, '极度高估'),
]


def _classify(value: float, thresholds: list) -> str:
    for low, high, label in thresholds:
        if low <= value < high:
            return label
    return 'N/A'


def interpret_valuation_indicators(data: dict) -> dict:
    result = {'code': data.get('code', data.get('股票代码', ''))}

    pe_ttm = _find_numeric(data, ['pe_ttm', '市盈率-TTM', '市盈率(TTM)', '市盈率(动)', '市盈率-动态', 'pe'])
    if pe_ttm is not None and pe_ttm > 0:
        result['pe_ttm'] = round(pe_ttm, 2)
        result['pe_ttm_level'] = _classify(pe_ttm, PE_THRESHOLDS)

    pe_static = _find_numeric(data, ['pe_static', '市盈率-静态', '市盈率'])
    if pe_static is not None and pe_static > 0:
        result['pe_static'] = round(pe_static, 2)
        result['pe_static_level'] = _classify(pe_static, PE_THRESHOLDS)

    pb = _find_numeric(data, ['pb', '市净率', 'PB'])
    if pb is not None and pb > 0:
        result['pb'] = round(pb, 2)
        result['pb_level'] = _classify(pb, PB_THRESHOLDS)

    ps_ttm = _find_numeric(data, ['ps_ttm', '市销率', 'PS'])
    if ps_ttm is not None and ps_ttm > 0:
        result['ps_ttm'] = round(ps_ttm, 2)
        result['ps_ttm_level'] = _classify(ps_ttm, PS_THRESHOLDS)

    peg = _find_numeric(data, ['peg', 'PEG'])
    if peg is not None and peg > 0:
        result['peg'] = round(peg, 2)
        result['peg_level'] = _classify(peg, PEG_THRESHOLDS)

    dividend_yield = _find_numeric(data, ['dividend_yield', '股息率', 'dv_ratio'])
    if dividend_yield is not None and dividend_yield > 0:
        result['dividend_yield'] = round(dividend_yield, 2)
        result['dividend_yield_level'] = _classify(dividend_yield, DIVIDEND_YIELD_THRESHOLDS)

    ev_ebitda = _find_numeric(data, ['ev_ebitda', 'EV/EBITDA'])
    if ev_ebitda is not None and ev_ebitda > 0:
        result['ev_ebitda'] = round(ev_ebitda, 2)
        result['ev_ebitda_level'] = '低估' if ev_ebitda < 10 else '合理' if ev_ebitda < 15 else '偏高'

    return result


def calc_industry_compare(stock_valuation: dict, industry_data: dict) -> dict:
    result = {
        'industry': industry_data.get('行业', industry_data.get('industry', '')),
        'stock_count': industry_data.get('股票数量', industry_data.get('stock_count', 0)),
    }

    stock_pe = stock_valuation.get('pe_ttm') or stock_valuation.get('pe_static')
    industry_pe_mean = _find_numeric(industry_data, ['PE均值', 'pe_mean', '市盈率(算术平均)', '市盈率-动态'])
    industry_pe_median = _find_numeric(industry_data, ['PE中位数', 'pe_median'])

    if stock_pe and industry_pe_mean is not None and industry_pe_mean != 0:
        result['stock_pe'] = round(stock_pe, 2)
        result['industry_pe_mean'] = round(industry_pe_mean, 2)
        result['industry_pe_median'] = round(industry_pe_median, 2) if industry_pe_median else None
        result['pe_deviation_pct'] = round((stock_pe - industry_pe_mean) / abs(industry_pe_mean) * 100, 2)
        result['pe_relative'] = _relative_judge(result['pe_deviation_pct'])

    stock_pb = stock_valuation.get('pb')
    industry_pb_mean = _find_numeric(industry_data, ['PB均值', 'pb_mean', '市净率PB(整体法)', '市净率'])
    industry_pb_median = _find_numeric(industry_data, ['PB中位数', 'pb_median'])

    if stock_pb and industry_pb_mean is not None and industry_pb_mean != 0:
        result['stock_pb'] = round(stock_pb, 2)
        result['industry_pb_mean'] = round(industry_pb_mean, 2)
        result['industry_pb_median'] = round(industry_pb_median, 2) if industry_pb_median else None
        result['pb_deviation_pct'] = round((stock_pb - industry_pb_mean) / abs(industry_pb_mean) * 100, 2)
        result['pb_relative'] = _relative_judge(result['pb_deviation_pct'])

    return result


def calc_percentile(history_df: pd.DataFrame, current_valuation: dict, years: int = 5) -> dict:
    result = {'years': years}

    pe_col = _find_column(history_df, ['pe_ttm', '市盈率-TTM', '市盈率(TTM)', '市盈率(动)', '市盈率-动态', 'pe'])
    if pe_col and pe_col in history_df.columns:
        pe_series = pd.to_numeric(history_df[pe_col], errors='coerce')
        pe_series = pe_series[pe_series > 0].dropna()
        if len(pe_series) > 10:
            current_pe = current_valuation.get('pe_ttm') or current_valuation.get('pe_static')
            if current_pe and current_pe > 0:
                result['pe_percentile'] = round(float((pe_series < current_pe).sum() / len(pe_series) * 100), 2)
                result['pe_level'] = _classify(result['pe_percentile'], PERCENTILE_LEVELS)
                result['pe_min'] = round(float(pe_series.min()), 2)
                result['pe_max'] = round(float(pe_series.max()), 2)
                result['pe_median'] = round(float(pe_series.median()), 2)

    pb_col = _find_column(history_df, ['pb', '市净率', 'PB'])
    if pb_col and pb_col in history_df.columns:
        pb_series = pd.to_numeric(history_df[pb_col], errors='coerce')
        pb_series = pb_series[pb_series > 0].dropna()
        if len(pb_series) > 10:
            current_pb = current_valuation.get('pb')
            if current_pb and current_pb > 0:
                result['pb_percentile'] = round(float((pb_series < current_pb).sum() / len(pb_series) * 100), 2)
                result['pb_level'] = _classify(result['pb_percentile'], PERCENTILE_LEVELS)
                result['pb_min'] = round(float(pb_series.min()), 2)
                result['pb_max'] = round(float(pb_series.max()), 2)
                result['pb_median'] = round(float(pb_series.median()), 2)

    return result


def calc_dcf(symbol: str, financial_data: dict, current_price: float,
             growth_rate: float = 0.08, wacc: float = 0.10,
             terminal_growth: float = 0.03, forecast_years: int = 5) -> dict:
    fcf = _estimate_fcf(financial_data)
    if fcf is None or fcf <= 0:
        return {'error': '无法估算自由现金流，数据不足', 'symbol': symbol}

    projected_fcf = []
    current_fcf = fcf
    for i in range(forecast_years):
        current_fcf = current_fcf * (1 + growth_rate)
        projected_fcf.append(current_fcf)

    pv_fcf = sum(fcf_i / (1 + wacc) ** (i + 1) for i, fcf_i in enumerate(projected_fcf))

    terminal_value = projected_fcf[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / (1 + wacc) ** forecast_years

    total_value = pv_fcf + pv_terminal

    shares = _find_numeric(financial_data, ['total_shares', '总股本', 'totalShare'])
    if shares and shares > 0:
        intrinsic_per_share = total_value / shares * 1e8 if shares > 1e8 else total_value / shares
    else:
        intrinsic_per_share = total_value

    safety_margin = ((intrinsic_per_share - current_price) / current_price * 100) if current_price > 0 else 0

    return {
        'symbol': symbol,
        'intrinsic_value': round(intrinsic_per_share, 2),
        'current_price': round(current_price, 2),
        'safety_margin': round(safety_margin, 2),
        'valuation_level': _safety_margin_level(safety_margin),
        'assumptions': {
            'estimated_fcf': round(fcf, 2),
            'growth_rate': growth_rate,
            'wacc': wacc,
            'terminal_growth': terminal_growth,
            'forecast_years': forecast_years,
        }
    }


def calc_ddm(symbol: str, financial_data: dict, current_price: float,
             growth_rate: float = 0.05, required_rate: float = 0.10) -> dict:
    dividend = _find_numeric(financial_data, ['dividend_per_share', '每股股利', 'dps', '股利'])
    if dividend is None or dividend <= 0:
        return {'error': '无法获取股利数据，DDM不适用', 'symbol': symbol}

    if required_rate <= growth_rate:
        return {'error': '要求回报率必须大于增长率', 'symbol': symbol}

    intrinsic_value = dividend * (1 + growth_rate) / (required_rate - growth_rate)
    safety_margin = ((intrinsic_value - current_price) / current_price * 100) if current_price > 0 else 0

    return {
        'symbol': symbol,
        'intrinsic_value': round(intrinsic_value, 2),
        'current_price': round(current_price, 2),
        'safety_margin': round(safety_margin, 2),
        'valuation_level': _safety_margin_level(safety_margin),
        'assumptions': {
            'dividend_per_share': round(dividend, 4),
            'growth_rate': growth_rate,
            'required_rate': required_rate,
        }
    }


def _find_numeric(data: dict, candidates: list):
    for key in candidates:
        if key in data:
            val = data[key]
            if isinstance(val, (int, float)):
                return val
            try:
                return float(val)
            except (ValueError, TypeError):
                import re
                m = re.search(r'[-+]?\d*\.?\d+', str(val))
                if m:
                    try:
                        return float(m.group())
                    except (ValueError, TypeError):
                        continue
    return None


def _find_column(df: pd.DataFrame, candidates: list) -> Optional[str]:
    for col in candidates:
        if col in df.columns:
            return col
    for col in df.columns:
        for c in candidates:
            if c.lower() in col.lower():
                return col
    return None


def _relative_judge(deviation_pct: float) -> str:
    if deviation_pct < -20:
        return '远低于行业'
    elif deviation_pct < -5:
        return '低于行业'
    elif deviation_pct <= 5:
        return '接近行业'
    elif deviation_pct <= 20:
        return '高于行业'
    else:
        return '远高于行业'


def _estimate_fcf(financial_data: dict) -> Optional[float]:
    net_profit = _find_numeric(financial_data, ['net_profit', '净利润', 'netProfit'])
    depreciation = _find_numeric(financial_data, ['depreciation', '折旧', 'depreciationAndAmortization'])
    capex = _find_numeric(financial_data, ['capex', '资本支出', 'capitalExpenditure'])
    working_capital_change = _find_numeric(financial_data, ['working_capital_change', '营运资金变动'])

    if net_profit is None:
        return None

    depreciation = depreciation or 0
    capex = capex or 0
    working_capital_change = working_capital_change or 0

    fcf = net_profit + depreciation - capex - working_capital_change
    return fcf if fcf > 0 else None


def _safety_margin_level(margin: float) -> str:
    if margin > 30:
        return '极度低估'
    elif margin > 15:
        return '低估'
    elif margin > -15:
        return '合理'
    elif margin > -30:
        return '偏高'
    else:
        return '高估'
