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

        self._get_limit_up_pool = _lup
        self._get_limit_break_pool = _lbp
        self._get_lianban_ladder = _ladder
        self._get_stock_anomaly = _anomaly
        self._get_dragon_tiger_list = _dtl
        self._get_hot_stocks = _hot
        self._get_stock_news = _news
        self._analyze_sentiment = analyze_sentiment

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
