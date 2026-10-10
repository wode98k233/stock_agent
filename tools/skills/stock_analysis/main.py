"""
选股雷达 - 个股分析技能（stock_analysis）

由 stock_query（板块/行情/历史K线/评级/财务/板块列表/新闻搜索 8 工具）
+ technical_analysis（技术指标 1 工具）
+ risk_metrics（风险指标 1 工具）合并而来，共 10 个工具。

合并动因（2026-09-20）：
1. **多源降级备源**：本技能经 `tools/fetcher` 取数，自带数据源降级链
   （实测 `mx_data → sina_direct → hithink → akshare` 自动切换）。
   `mx_data_query` 是妙想专用、无备源，因此个股取数的备源由本技能承担。
2. **解开「取数 + 计算」耦合**：原先取数（stock_query）与计算
   （technical_analysis / risk_metrics）分属三个技能，而三者本就服务同一场景
   （个股分析占真实提问约 38%），且 3 个技能都是「1-8 个工具」的碎片形态。
3. **消除碎片化**：3 个禁用技能 → 1 个启用技能，目录不膨胀。

工具名与合并前**完全一致**，因此 `tools/tool_capabilities.py::HK_US_ALLOWED_TOOLS`
中的 `calc_technical_indicators` 等白名单无需改动。
"""
import json
import traceback

from tools.skill_builder import SkillBuilder, skill_tool


def _history_fallback(symbol: str, reason: str, hint: str) -> dict:
    """历史K线不可用时的统一降级提示（沿用原两个技能的 retry:false 约定）。"""
    return {
        'symbol': symbol,
        'error': f'{symbol} 历史K线不可用: {reason}',
        'fallback': f'改用 mx_data_query 查询 "{hint}"，不要反复调用本工具',
        'retry': False,
    }


def _fetch_benchmark_history(code: str, days: int, logger):
    """基准指数历史：优先读本地 `index_daily`（毫秒级），不可用再回退 fetcher。

    背景（2026-09-21 用户实测卡住）：基准若走 `get_stock_history`（个股接口），
    专用K线源取不到指数代码，会一路重试到 `mx_data`，把 `get_risk_metrics`
    拖到分钟级。本地 `index_daily` 由指数采集任务维护（如 000300 有 275 行），
    读它几乎零成本。`calc_risk_metrics` 只要求 `close` 列，无需日期对齐。
    """
    try:
        from utils.cache.market_data_db import get_index_daily

        df = get_index_daily(code)
        if df is not None and not df.empty and 'close' in df.columns and len(df) >= 10:
            if 'trade_date' in df.columns:
                df = df.sort_values('trade_date')
            return df.tail(days).reset_index(drop=True)
    except Exception as e:
        logger.warning(f"本地指数日线读取失败 {code}: {e}")
    return None


# 通用网搜的装配 + 归一化已收敛到 tools/search/service.py（唯一装配点，走
# SearchProviderFactory 的 8 源）。原先这里的 `_NEWS_FALLBACK_HINT` /
# `_search_failure_payload` / `_normalize_search_response` 三个私有实现是
# 「两套实现各自漂移」的根源（本技能只认 MX/SerpAPI/Tavily，配置面板却暴露 8 源），
# 2026-10-04 统一下沉，技能侧改为调用 `service.run_web_search()`。


class StockAnalysisSkill(SkillBuilder):
    """个股分析技能：多源取数 + 技术指标 + 风险指标"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数（延迟导入，避免拖慢技能加载）"""
        from tools.stock_data import (
            get_board_stocks as _board,
            get_stock_realtime as _realtime,
            get_stock_history as _hist,
            get_stock_rating as _rating,
            get_stock_financial as _financial,
            get_industry_list as _ilist,
            get_concept_list as _clist,
        )
        self._get_board_stocks = _board
        self._get_stock_realtime = _realtime
        self._get_stock_history = _hist
        self._get_stock_rating = _rating
        self._get_stock_financial = _financial
        self._get_industry_list = _ilist
        self._get_concept_list = _clist

        from tools.tech_indicators import calc_indicators
        from tools.risk_calc import calc_risk_metrics

        self._calc_indicators = calc_indicators
        self._calc_risk_metrics = calc_risk_metrics

    # ── 基础数据查询（原 stock_query）────────────────────────

    @skill_tool
    def get_board_stocks(self, board_name: str) -> list:
        """获取板块/概念的成分股。输入板块名称如"电力"、"石油"、"人工智能"。"""
        df = self._get_board_stocks(board_name, self.logger)
        cols = [c for c in ['代码', '名称'] if c in df.columns]
        if cols:
            return df[cols].head(50).to_dict('records')
        return df.head(50).to_dict('records')

    @skill_tool
    def get_stock_realtime(self, symbol: str) -> dict:
        """获取个股实时行情快照。输入股票代码如"600519"。"""
        return self._get_stock_realtime(symbol, self.logger)

    @skill_tool
    def get_stock_history(self, symbol: str, days: int = 60, period: str = "daily") -> dict:
        """获取历史K线数据。输入股票代码、条数（默认 60）与周期 period。

        period 取 `daily`（默认）/ `weekly` / `monthly`——判周线/月线趋势时必须显式传
        weekly/monthly 单独取（返回最近 20 根与总条数）。周/月线不落缓存。
        """
        df = self._get_stock_history(symbol, days, self.logger, period)
        tail = df.tail(20).reset_index()
        tail['date'] = tail['date'].astype(str)
        return {'total_days': len(df), 'latest_5': tail.to_dict('records')}

    @skill_tool
    def get_stock_rating(self, symbol: str) -> dict:
        """获取机构评级。输入股票代码。"""
        return self._get_stock_rating(symbol, self.logger)

    @skill_tool
    def get_stock_financial(self, symbol: str) -> dict:
        """获取核心财务指标。输入股票代码。"""
        return self._get_stock_financial(symbol, self.logger)

    @skill_tool
    def get_industry_list(self) -> list:
        """获取所有行业板块列表。返回板块名称和代码，可用于查询板块成分股。"""
        df = self._get_industry_list(self.logger)
        return df.head(50).to_dict('records')

    @skill_tool
    def get_concept_list(self) -> list:
        """获取所有概念板块列表。返回板块名称和代码，可用于查询板块成分股。"""
        df = self._get_concept_list(self.logger)
        return df.head(50).to_dict('records')

    @skill_tool
    def search_stock_news(self, query: str, max_results: int = 5) -> dict:
        """搜索个股相关新闻和资讯（多引擎聚合，通用网搜）。输入搜索关键词如"贵州茅台 最新消息"或"中际旭创 业绩"。

        走 `tools/search/service.py::run_web_search`（唯一装配点，按配置自动启用
        MX/Tavily/SerpAPI/Bocha/Brave/Anspire/MiniMax/SearXNG 中所有可用源）。
        与 `web_search` 技能是**同一条链路**：本工具语义偏「个股/板块 资讯」，
        `web_search` 偏「通用信息检索」，按场景二选一即可，不要同时调两个。
        返回 {query, success, result_count, results[], provider, error?, hint?}。
        """
        from tools.search.service import run_web_search
        return run_web_search(query, max_results=max_results, timeout=30)

    # ── 技术指标（原 technical_analysis）────────────────────

    @skill_tool
    def calc_technical_indicators(self, symbol: str) -> dict:
        """计算全部技术指标(MACD/MA/RSI/BB/KDJ/OBV/ATR/CCI/WR/DMI/PSY/VR等)。输入股票代码。包含趋势字段（RSI趋势、MACD柱趋势、金叉/死叉天数等）。"""
        try:
            df = self._get_stock_history(symbol, 120, self.logger)
        except Exception as e:
            return _history_fallback(symbol, str(e), f"{symbol} MA MACD RSI KDJ 收盘价 成交量")

        required = {'close', 'high', 'low', 'volume'}
        if df.empty or not required.issubset(set(df.columns)):
            missing = sorted(required.difference(set(df.columns)))
            reason = '数据为空' if df.empty else f'缺少字段 {missing}'
            return _history_fallback(symbol, reason, f"{symbol} MA MACD RSI KDJ 收盘价 成交量")

        try:
            ind = self._calc_indicators(df)
        except Exception as e:
            return _history_fallback(symbol, f'指标计算失败: {e}', f"{symbol} MA MACD RSI KDJ 收盘价 成交量")
        ind['code'] = symbol
        return ind

    # ── 风险指标（原 risk_metrics）──────────────────────────

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

        hint = f"{symbol} 近120日收盘价 波动率 最大回撤"
        try:
            df = self._get_stock_history(symbol, days, self.logger)
        except Exception as e:
            return _history_fallback(symbol, str(e), hint)

        if df.empty or len(df) < 10:
            return _history_fallback(symbol, f'历史数据不足，只有 {len(df)} 个交易日', hint)
        if 'close' not in df.columns:
            return _history_fallback(symbol, f'缺少字段 close, columns={list(df.columns)}', hint)

        benchmark_df = _fetch_benchmark_history(benchmark, days, self.logger)
        if benchmark_df is None:
            try:
                benchmark_df = self._get_stock_history(benchmark, days, self.logger)
            except Exception:
                self.logger.warning(f"基准 {benchmark} 数据获取失败（本地指数库与 fetcher 均不可用），跳过 Beta 计算")

        metrics = self._calc_risk_metrics(df, benchmark_df)
        metrics['symbol'] = symbol
        metrics['benchmark'] = benchmark if benchmark_df is not None and not benchmark_df.empty else None
        set_risk_metrics_cache(cache_key, metrics)
        return metrics

    # ── 筹码分布（2026-09-21 新增：妙想额度耗尽时的备源）────────────

    @skill_tool
    def get_chip_distribution(self, symbol: str) -> dict:
        """获取个股筹码分布：获利比例、平均成本、90%成本区间、90%集中度（另附 70% 口径）。输入股票代码如"603369"。

        免费源（AKShare → 东财 `stock_cyq_em`），**不消耗妙想额度**；
        是「筹码分布只有妙想一个来源」的备源：妙想额度耗尽时用它补齐，
        字段口径与报告模板「筹码分布」板块要求一一对应。
        """
        from utils.cache import get_chip_distribution_cache, set_chip_distribution_cache

        cached = get_chip_distribution_cache(symbol)
        if cached is not None:
            return cached

        try:
            from tools.fetcher import ak_chip_distribution
            data = ak_chip_distribution(symbol)
        except Exception as e:
            return {
                'symbol': symbol,
                'error': f'{symbol} 筹码分布不可用: {e}',
                'hint': '可改用 mx_data_query 查「筹码分布 获利比例 平均成本 90%成本区间 集中度」',
            }

        if not isinstance(data, dict) or not data:
            return {'symbol': symbol, 'error': f'{symbol} 筹码分布返回为空'}

        data.setdefault('symbol', symbol)
        data['source'] = 'akshare'
        # 报告「筹码分布」板块直接消费的四个字段，显式给出中文键，降低模型取错键的概率
        data['fields'] = {
            '获利比例': data.get('profit_ratio'),
            '平均成本': data.get('avg_cost'),
            '90%成本区间': [data.get('cost_90_low'), data.get('cost_90_high')],
            '90%集中度': data.get('concentration_90'),
        }
        set_chip_distribution_cache(symbol, data)
        return data

    # ── 估值历史分位（2026-09-21 新增：pe_percentile 的上游）────────

    @skill_tool
    def get_valuation_percentile(self, symbol: str, years: int = 5) -> dict:
        """获取个股 PE(TTM)/PB 的历史分位（近 N 年，默认 5 年）。输入股票代码如"603369"。

        输出 `pe_percentile` / `pb_percentile` / `pe_ttm` / `pb` 及区间统计，
        正是报告「估值与基本面」板块要求的「PE、PB 及其历史分位」，
        也补齐了分析管线里 `pe_percentile` 数据槽长期取不到值的缺口。
        数据源：fetcher 估值历史链（百度股市通 PE/PB 日频序列，含多源降级）；
        序列落本地缓存（`cache_valuation_history`），分位用 `tools/valuation.calc_percentile` 计算。

        **仅适用于个股**：ETF/LOF/REITs 等场内基金没有 PE(TTM)/PB 概念，调用会返回
        「不适用」提示；基金应改为分析其跟踪指数（如国证/中证指数）的估值分位。
        """
        import pandas as pd

        from tools.valuation import calc_percentile, is_etf_code
        from utils.cache import (get_valuation_history_cache,
                                 set_valuation_history_cache)

        if is_etf_code(symbol):
            # ETF 与个股不同：没有 PE(TTM)/PB 口径 → 明确「不适用」而不是报 error，
            # 避免污染工具失败率与报告里「数据缺失」的判定。
            return {
                'symbol': symbol,
                'applicable': False,
                'note': f'{symbol} 是场内基金（ETF/LOF），没有 PE(TTM)/PB 口径，估值历史分位不适用',
                'hint': '基金请改为分析其跟踪指数的估值分位（如中证/国证指数），或查该指数 PE 与分位',
            }

        years = int(years) if years else 5
        cached = get_valuation_history_cache(symbol, years)
        if cached is not None:
            df = pd.DataFrame(cached) if isinstance(cached, list) else cached
        else:
            try:
                from tools.fetcher import ak_valuation_history
                df = ak_valuation_history(symbol, years)
            except Exception as e:
                return {
                    'symbol': symbol,
                    'error': f'{symbol} 历史估值序列不可用: {e}',
                    'hint': '可改用 mx_data_query 查「PE PE(TTM) PB 历史估值分位」',
                }
            if df is None or df.empty:
                return {'symbol': symbol, 'error': f'{symbol} 历史估值序列为空'}
            try:
                set_valuation_history_cache(symbol, years, df.to_dict('records'))
            except Exception:
                pass  # 缓存失败不影响本次结果

        # 当前值直接取序列末行（与行情快照的 PE/PB 同源，避免跨源口径打架）
        current = {}
        for col, key in (('pe_ttm', 'pe_ttm'), ('pb', 'pb')):
            if col in df.columns:
                series = pd.to_numeric(df[col], errors='coerce').dropna()
                if not series.empty:
                    current[key] = float(series.iloc[-1])

        result = calc_percentile(df, current, years)
        result.update({
            'symbol': symbol,
            'source': 'akshare',
            'history_rows': int(len(df)),
            **current,
        })
        if 'trade_date' in df.columns:
            try:
                result['as_of'] = str(pd.to_datetime(df['trade_date']).max().date())
            except Exception:
                pass
        return result


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = StockAnalysisSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
