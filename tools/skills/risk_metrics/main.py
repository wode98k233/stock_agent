"""
选股雷达 - 风险指标分析技能
基于历史价格数据计算风险指标
"""
from tools.skill_builder import SkillBuilder, skill_tool


def _history_fallback(symbol: str, reason: str) -> dict:
    return {
        'symbol': symbol,
        'error': f'{symbol} 历史K线不可用: {reason}',
        'fallback': f'改用 mx_data_query 查询 "{symbol} 近120日收盘价 波动率 最大回撤"，不要反复调用 risk_metrics',
        'retry': False,
    }


class RiskMetricsSkill(SkillBuilder):
    """风险指标分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        from tools.stock_data import get_stock_history
        from tools.risk_calc import calc_risk_metrics
        self._get_stock_history = get_stock_history
        self._calc_risk_metrics = calc_risk_metrics

    def _unwrap(self, result):
        if isinstance(result, str):
            try:
                import json
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {'raw': result}
        return result

    @skill_tool
    def get_risk_metrics(self, symbol: str, days: int = 120, benchmark: str = "000300") -> dict:
        """
        计算个股风险指标。包含Beta、波动率、最大回撤、夏普比率等。
        symbol: 股票代码，days: 计算天数(默认120)，benchmark: 基准指数(默认沪深300)
        """
        from utils.cache import get_risk_metrics_cache, set_risk_metrics_cache
        cache_key = f'{symbol}_{days}_{benchmark}'
        cached = get_risk_metrics_cache(cache_key)
        if cached is not None:
            return cached
        try:
            df = self._get_stock_history(symbol, days, self.logger)
        except Exception as e:
            return _history_fallback(symbol, str(e))

        if df.empty or len(df) < 10:
            return _history_fallback(symbol, f'历史数据不足，只有 {len(df)} 个交易日')
        if 'close' not in df.columns:
            return _history_fallback(symbol, f'缺少字段 close, columns={list(df.columns)}')

        benchmark_df = None
        try:
            benchmark_df = self._get_stock_history(benchmark, days, self.logger)
        except Exception:
            self.logger.warning(f"基准 {benchmark} 数据获取失败，跳过 Beta 计算")

        metrics = self._calc_risk_metrics(df, benchmark_df)
        metrics['symbol'] = symbol
        metrics['benchmark'] = benchmark if benchmark_df is not None and not benchmark_df.empty else None
        set_risk_metrics_cache(cache_key, metrics)
        return metrics


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = RiskMetricsSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
