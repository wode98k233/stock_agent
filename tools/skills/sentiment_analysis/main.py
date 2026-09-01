"""
选股雷达 - 情感分析技能（重构版）
使用新的 SkillBuilder 抽象
"""
from tools.skill_builder import SkillBuilder, skill_tool
import json


class SentimentAnalysisSkill(SkillBuilder):
    """情感分析技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数"""
        from tools.sentiment import analyze_sentiment
        from tools.stock_data import get_stock_news as _news
        self._analyze_sentiment = analyze_sentiment
        self._get_stock_news = _news

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
    """构建工具列表（兼容接口）"""
    skill = SentimentAnalysisSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
