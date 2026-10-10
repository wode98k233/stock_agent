"""
选股雷达 - 资金流向分析技能
支持: 个股资金流向、北向资金（板块资金流向已移交 sector_rotation 技能，避免重复定义）

数据源策略（2026-09-20 新增兜底）
--------------------------------
主源 AKShare；**主源返回空表或抛异常时，个股资金流向会回落到同花顺问财**
（复用 `tools/other_skills/10jqka/skills.py::_iwc_query_core`，HTTP 实现单一事实源）。

兜底**刻意只放在内部 skill 的 tool 实现里**，不加在外部数据源 skill（`mx_*`）上：
保持「`mx_*` = 外部数据源 / 内部 skill = 分析取数能力（可自行组合多源）」的定位不混淆。

起因：2026-09-20 个股深度分析 run（中国中免）中妙想额度耗尽，资金面 4 次查询全失败，
报告的资金面板块只能标注「数据缺失」——而这类数据正是本技能该覆盖的。
"""
import json
import math
import re

import pandas as pd

from tools.skill_builder import SkillBuilder, skill_tool

# 问财技能模块的惰性加载缓存。
# 目录名 `10jqka` 以数字开头，`import tools.other_skills.10jqka.skills` 是语法错误，
# 只能用 importlib.import_module；False 表示「已尝试且失败」，避免每次都重试。
_IWENCAI_SKILLS = None

# 上报给问财的 skill id：问财配额口径是「每 skill 每天 100 次」，
# 用真实技能名而非自造 id，避免多域额度被挤进同一个桶（见 utils/skill_ctx.py）。
_IWENCAI_SKILL_ID = "hithink-market-query"

# 兜底问句：2026-09-20 实测一次调用即返回全部所需字段
# （主力资金流向 / 特大单净买入额 / 大单净买入额 / 近5日主力累计），故兜底只花 1 次配额。
_FUND_FLOW_QUERY = "{symbol} 主力净流入 超大单净流入 大单净流入 近5日主力净流入"


def _load_iwencai_skills():
    """惰性加载问财技能模块；不可用时返回 None（不抛异常）。"""
    global _IWENCAI_SKILLS
    if _IWENCAI_SKILLS is None:
        try:
            import importlib

            _IWENCAI_SKILLS = importlib.import_module("tools.other_skills.10jqka.skills")
        except Exception:
            _IWENCAI_SKILLS = False
    return _IWENCAI_SKILLS or None


def _iwencai_datas(query: str):
    """调问财 query2data 并返回 datas 列表；任何失败返回 None（绝不抛）。"""
    mod = _load_iwencai_skills()
    if mod is None:
        return None
    try:
        raw = mod._iwc_query_core(query, skill_id=_IWENCAI_SKILL_ID)
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    inner = payload.get("data") or {}
    datas = inner.get("datas") if isinstance(inner, dict) else None
    return datas or None


class MoneyFlowSkill(SkillBuilder):
    """资金流向分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.fetcher import (
            ak_individual_fund_flow,
            ak_north_fund_flow,
        )
        self._get_individual_fund_flow = ak_individual_fund_flow
        self._get_north_fund_flow = ak_north_fund_flow

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json as _json
                return _json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @staticmethod
    def _safe_frame(fn, *args) -> pd.DataFrame:
        """主源调用包一层：异常视为「无数据」返回空表，不向上抛。

        原实现直接调用，fetcher 抖动（限流/接口变更）会让整个 tool 抛异常，
        调用方拿不到任何可降级的信息。
        """
        try:
            df = fn(*args)
        except Exception:
            return pd.DataFrame()
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    # 北向宽表转长表时优先保留的指标关键词（命中即保留，顺序即优先级）
    _NORTH_LABEL_HINTS = ("净买", "净流入", "成交额", "累计", "余额", "持股市值", "增持")

    @staticmethod
    def _to_native(value):
        """numpy/pandas 标量 → 原生类型；NaN → None（保证 JSON 可序列化）。"""
        if value is None:
            return None
        try:
            if isinstance(value, float) and math.isnan(value):
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return str(value)
        return value

    @classmethod
    def _north_flow_records(cls, df: pd.DataFrame, limit: int = 20,
                            max_labels: int = 8) -> list:
        """把不同数据源的北向返回统一成「一行一交易日」的长表。

        妙想源返回的是**宽表**（指标为行、日期为列，列名就是 "2026-09-21" 这类
        日期串），直接 `tail(10).to_dict('records')` 会得到
        `{"date": "上证指数涨跌幅(%)", "2026-09-21": "0.97", ...}` 这种不可读结构，
        而且体积随列数膨胀（线上实测 55KB，被上下文截断后彻底不可用）。
        这里统一：宽表转置 → 只保留资金相关指标 → 限制条数与指标数。
        """
        if df is None or df.empty:
            return []

        date_cols = [c for c in df.columns
                     if isinstance(c, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", c)]

        if len(date_cols) >= 2:
            # 宽表：列 = 日期，行 = 指标（第一列通常是指标名）
            label_col = next((c for c in df.columns if c not in date_cols), df.columns[0])
            labels = [str(r.get(label_col, "")).strip() for _, r in df.iterrows()]
            keep = [i for i, lb in enumerate(labels)
                    if any(h in lb for h in cls._NORTH_LABEL_HINTS)]
            if not keep:
                keep = list(range(len(labels)))[:max_labels]
            keep = keep[:max_labels]
            by_date: dict = {}
            for i in keep:
                label = labels[i] or f"指标{i}"
                row = df.iloc[i]
                for col in date_cols:
                    val = cls._to_native(row.get(col))
                    if val is None:
                        continue
                    by_date.setdefault(col, {})[label] = val
            ordered = sorted(by_date.items())[-limit:]
            return [{"date": d, **vals} for d, vals in ordered]

        # 长表：按日期升序取尾部
        date_col = next((c for c in ("date", "日期", "trade_date") if c in df.columns), None)
        if date_col is not None:
            try:
                df = df.sort_values(date_col)
            except Exception:
                pass
        return [
            {str(k): cls._to_native(v) for k, v in rec.items()}
            for rec in df.tail(limit).to_dict("records")
        ]

    @staticmethod
    def _stock_fund_flow_via_iwencai(symbol: str):
        """问财兜底：个股主力/特大单/大单净额 + 近5日主力累计。取不到返回 None。"""
        datas = _iwencai_datas(_FUND_FLOW_QUERY.format(symbol=symbol))
        if not datas or not isinstance(datas[0], dict):
            return None
        row = datas[0]
        return {
            'symbol': symbol,
            'stock_name': row.get('股票简称'),
            'fund_flow': datas[:5],
            'total_days': len(datas),
            'source': 'iwencai',
            'note': 'AKShare 主源未返回数据，已回落到同花顺问财；字段名为问财口径'
                    '（主力资金流向 / 特大单净买入额 / 大单净买入额 / 区间主力累计）',
        }

    @skill_tool
    def get_stock_fund_flow(self, symbol: str) -> dict:
        """获取个股资金流向（主力/散户/净流入）。输入股票代码如"600519"。

        主源 AKShare；取不到时自动回落到同花顺问财（含近5日主力累计方向）。
        """
        from utils.cache import get_fund_flow_cache, set_fund_flow_cache
        cached = get_fund_flow_cache(symbol)
        if cached is not None:
            return cached

        df = self._safe_frame(self._get_individual_fund_flow, symbol)
        if not df.empty:
            result = {
                'symbol': symbol,
                'fund_flow': df.tail(5).to_dict('records'),
                'total_days': len(df),
                'source': 'akshare',
            }
            set_fund_flow_cache(symbol, result)
            return result

        fallback = self._stock_fund_flow_via_iwencai(symbol)
        if fallback is not None:
            set_fund_flow_cache(symbol, fallback)
            return fallback
        return {'error': f'未获取到 {symbol} 的资金流向数据（AKShare 与问财均无返回）'}

    # 注：板块资金流向（get_sector_fund_flow）已从本技能移除——它与
    # tools/skills/sector_rotation 的同名工具调用**同一个** `ak_sector_fund_flow_rank`，
    # 属重复定义（2026-09-20 去重）。保留 sector_rotation 的实现，因为它带
    # `_run_with_timeout` 超时保护（实测板块行情接口确会超时）。
    # 板块资金流请用 sector_rotation.get_sector_fund_flow。

    @skill_tool
    def get_north_fund_flow(self, symbol: str = "北向资金") -> dict:
        """获取北向资金历史数据（**市场级**口径）。symbol可选"北向资金"/"沪股通"/"深股通"。

        返回近 20 个交易日的长表（一行一交易日，键为资金指标），已做体积约束。
        注：本工具是**市场级**北向资金序列。问财的北向问句返回的是「个股净买入额
        排行」（market 级语义不同构），故此处不做问财兜底，避免把个股排行当成
        市场资金流序列返回给调用方；**个股**北向净买入明细不在本工具能力范围内。
        """
        from utils.cache import get_fund_flow_cache, set_fund_flow_cache
        cache_key = f'north_{symbol}'
        cached = get_fund_flow_cache(cache_key)
        if cached is not None:
            return cached
        df = self._safe_frame(self._get_north_fund_flow, symbol)
        if df.empty:
            return {
                'error': f'未获取到 {symbol} 的北向资金数据',
                'scope': 'market',
                'note': f'北向资金为市场级口径；{symbol} 本次无返回。'
                        f'该缺口不影响个股自身资金流（get_stock_fund_flow 覆盖）。',
            }
        records = self._north_flow_records(df, limit=20)
        result = {
            'symbol': symbol,
            'scope': 'market',
            'note': '北向资金为市场级口径（沪股通/深股通/北向资金汇总序列）；'
                    '个股北向净买入明细不在本工具能力范围内，勿据此判断个股外资进出。',
            'history': records,
            'returned_days': len(records),
            'source': 'fetcher',
        }
        # 口径自检（2026-09-21 实测踩到）：源可能返回「年度合计」这类**非日频**数据
        # （实测 date='2024-12-31(年)' + 列 '成交额(合计)'）—— 结构化没问题，
        # 但拿它当日频净买入用会得出完全错误的结论，必须显式标注。
        yearly = [r for r in records
                  if isinstance(r.get('date'), str) and '(年)' in r['date']]
        if records and len(yearly) == len(records):
            result['granularity'] = 'yearly'
            result['note'] += (' ⚠️ 本次源返回的是**年度合计**（date 带「(年)」），'
                               '不是日频净买入，禁止用于日频资金验证。')
        elif yearly:
            result['granularity'] = 'mixed'
            result['note'] += f' ⚠️ 其中 {len(yearly)} 行为年度合计，勿与日频数据混用。'
        else:
            result['granularity'] = 'daily'
        set_fund_flow_cache(cache_key, result)
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = MoneyFlowSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
