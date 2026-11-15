"""
选股雷达 - 盘面复盘技能（market_review）

由 special_data（涨停池/炸板池/连板天梯/个股异动/龙虎榜/热股榜）
与 sentiment_analysis（新闻情感打分/关键词筛选）合并而来。

合并动因（2026-09-20）：合并前两者分别只有 6 个和 2 个工具，且
`special_data` 的 SKILL.md 缺少「使用指南」；两者都在服务「盘面复盘」
这一类任务（复盘类提问占真实提问的 20%），拆成两个技能只会让 planner
在 27 个技能里做更多无效选择。工具名保持与合并前完全一致，避免影响
tools/tool_capabilities.py 的市场白名单与历史 trace。
"""
import json

from tools.skill_builder import SkillBuilder, skill_tool


class MarketReviewSkill(SkillBuilder):
    """盘面复盘技能：特色盘面数据 + 新闻情感"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数（延迟导入，避免拖慢技能加载）"""
        from tools.stock_data import (
            get_limit_up_pool as _lup,
            get_limit_break_pool as _lbp,
            get_lianban_ladder as _ladder,
            get_stock_anomaly as _anomaly,
            get_dragon_tiger_list as _dtl,
            get_hot_stocks as _hot,
            get_stock_news as _news,
        )
        from tools.sentiment import analyze_sentiment
        from tools.market_snapshot import build_market_snapshot as _snapshot

        self._get_limit_up_pool = _lup
        self._get_limit_break_pool = _lbp
        self._get_lianban_ladder = _ladder
        self._get_stock_anomaly = _anomaly
        self._get_dragon_tiger_list = _dtl
        self._get_hot_stocks = _hot
        self._get_stock_news = _news
        self._analyze_sentiment = analyze_sentiment
        self._build_market_snapshot = _snapshot

    # ── 盘面特色数据 ────────────────────────────────────────

    @skill_tool
    def get_limit_up_pool(self, date: str = None, n: int = 20) -> list:
        """获取涨停池（含涨停时间、涨停原因、连板数、封单额，按连板数降序）。date 如"2026-08-21"，默认最近交易日。"""
        return self._get_limit_up_pool(date, n, self.logger)

    @skill_tool
    def get_limit_break_pool(self, date: str = None, n: int = 20) -> list:
        """获取炸板池（涨停开板未封住，含开板次数）。date 如"2026-08-21"，默认最近交易日。"""
        return self._get_limit_break_pool(date, n, self.logger)

    @skill_tool
    def get_lianban_ladder(self, days: int = 5) -> list:
        """获取连板天梯（近 N 交易日连板梯队矩阵，观察晋级/断板）。days 默认 5。"""
        return self._get_lianban_ladder(days, self.logger)

    @skill_tool
    def get_stock_anomaly(self, tag_codes: str = "", n: int = 20) -> list:
        """获取当日个股异动原因列表（含解读与关键词）。tag_codes 逗号分隔 OR：LIMIT_UP/LIMIT_DOWN/SHARP_RISE/SHARP_FALL/RAPID_RALLY/RAPID_DECLINE，留空返回全部。"""
        return self._get_stock_anomaly(None, n, tag_codes, self.logger)

    @skill_tool
    def get_dragon_tiger_list(self, date: str = None, n: int = 20, board_type: str = "all") -> list:
        """获取龙虎榜（净买入/买卖金额/机构席位）。board_type: all 全部 / org 机构榜 / hot_money 游资榜。"""
        return self._get_dragon_tiger_list(date, n, board_type, self.logger)

    @skill_tool
    def get_hot_stocks(self, n: int = 10) -> list:
        """获取 A 股热股榜（24h 人气排名）。"""
        return self._get_hot_stocks(n, self.logger)

    # ── 市场广度 ────────────────────────────────────────────

    @skill_tool
    def get_market_breadth(self) -> dict:
        """获取沪深京全市场广度快照（全市场成交额/涨跌家数/涨停跌停，含上一交易日对比）。

        口径与纪律（必须遵守）：
        - amount 为沪深京三市全市场成交额合计（含北交所，单位：元，盘中为当日累计值）。
          它不是任何指数的成交额：禁止用上证指数/深证成指/创业板指等指数成交额相加
          或用它代替市场总量——指数成交额只代表其成分股。
        - prev_amount 为上一交易日全市场成交额（同口径，经交易日历校验），可能为 null
          （首次运行/缓存过期/上一交易日缺失）；为 null 时不得下无标签的量能定性结论——
          可用带口径与来源标注的替代基准（如媒体口径）兜底并注明置信度打折，
          无任何可比基准时只能写「缺少可比基准」。
        - 盘中 amount 与 prev_amount（全天值）对比时必须写明折算方式（如「按半日折算」）。
        - 全市场快照不可用时返回空对象 {}，不要反复重试；改用 iwc_query 补充确认。
        - 涨停/跌停为「按各板块涨跌停价逐股复算」口径（dist.limit_up / dist.limit_down，含 ST）；
          limit_up_excl_st / limit_down_excl_st 是剔 ST 后与东财官方涨停/跌停池对齐的口径。
          禁止用「涨幅 >= 9.8%」一类阈值替代——创业板 20%、北交所 30%、低价股四舍五入后
          主板可达 9.615%~12.000%，阈值法会双向失真。
        """
        from tools.fetcher.hithink_ds import HithinkDataSource

        df = HithinkDataSource.get_spot_all()
        names = {}
        try:
            from utils.cache.market_data_db import get_stock_name_map

            names = get_stock_name_map()
        except Exception:
            pass
        return self._build_market_snapshot(df, names) or {}

    # ── 新闻情感 ────────────────────────────────────────────

    @skill_tool
    def analyze_news_sentiment(self, symbol_or_news: str) -> dict:
        """分析新闻情感。输入股票代码(自动获取新闻)或JSON格式新闻列表。"""
        if symbol_or_news.startswith('['):
            news = json.loads(symbol_or_news)
        else:
            news = self._get_stock_news(symbol=symbol_or_news, logger=self.logger)
        if not news:
            return {'total_score': 0, 'conclusion': '中性', 'summary': '无新闻'}
        return self._analyze_sentiment(news, self.memory_mgr, self.logger)

    @skill_tool
    def search_news_by_keyword(self, keyword: str, symbol: str) -> str:
        """在个股新闻中搜索关键词。输入关键词和股票代码。"""
        news = self._get_stock_news(symbol=symbol, logger=self.logger)
        result = [n for n in news if keyword in n.get('title', '') or keyword in n.get('content', '')][:10]
        # 返回 JSON 字符串以保持与原实现一致
        return json.dumps(result, ensure_ascii=False)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = MarketReviewSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
