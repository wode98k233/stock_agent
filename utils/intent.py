"""
选股雷达 - 意图记忆系统
记录用户的关注领域和偏好，提供个性化分析

与 MemoryManager 的关系：
  - IntentMemoryManager 独立存储，不依赖 MemoryManager
  - MemoryManager 管理对话历史（LangChain Buffer）
  - IntentMemoryManager 管理意图画像（结构化权重数据）
  - 两者互补，共同构成完整的用户记忆体系
"""
import json
import os
import time
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

from utils.app_paths import get_data_path


KNOWN_SECTORS = [
    "电池", "券商", "银行", "房地产", "汽车", "医药", "科技",
    "新能源", "半导体", "消费", "白酒", "军工", "光伏", "锂电",
    "人工智能", "AI", "芯片", "5G", "稀土", "氢能", "储能",
    "新材料", "碳中和", "数字经济", "机器人", "无人驾驶", "虚拟现实",
]

KNOWN_SUFFIXES = [
    "股份", "科技", "集团", "银行", "证券", "保险", "能源", "电子",
    "医药", "锂电", "汽车", "材料", "信息", "生物", "环保", "电力",
    "化工", "钢铁", "有色", "地产", "物业", "航空", "港口", "传媒",
    "教育", "网络", "数据", "智能", "光伏", "储能", "芯片", "通信",
    "软件", "医疗", "电器", "装备", "重工", "矿业", "水泥", "玻璃",
    "造纸", "纺织", "食品", "饮料", "酒", "乳业", "农牧", "渔业",
    "林业", "旅游", "酒店", "餐饮", "影视", "游戏", "广告", "物流",
    "快递", "电商", "零售", "超市", "百货", "家电", "家居", "建材",
    "装修", "园林", "设计", "咨询", "培训", "检测", "认证", "租赁",
    "保理", "信托", "基金", "期货", "资管", "资本", "控股", "发展",
    "投资", "实业", "商业", "贸易", "建设", "工程", "管理", "服务",
]


@dataclass
class UserIntent:
    """用户意图画像"""
    watched_sectors: Dict[str, float] = field(default_factory=dict)
    watched_stocks: Dict[str, float] = field(default_factory=dict)
    question_types: Dict[str, int] = field(default_factory=dict)
    preferred_depth: str = "medium"
    recent_queries: List[dict] = field(default_factory=list)
    last_updated: float = 0.0
    DECAY_FACTOR: float = 0.95
    MAX_RECENT: int = 20


class IntentMemoryManager:
    """
    意图记忆管理器（完全独立存储，不依赖 MemoryManager）

    存储设计：
      - 目录：data/intent/
      - 文件：{user_id}.json（默认为 default.json）
      - 格式：asdict(UserIntent) 的 JSON

    设计原则：
      1. 完全独立：不修改任何现有类的实现
      2. 容错优先：所有 I/O 操作都有 try/except，失败不影响主流程
      3. 衰减机制：板块/个股权重每次更新乘以 DECAY_FACTOR，自然遗忘
      4. 增量更新：不是覆盖，而是叠加调整
    """

    DEFAULT_USER = "default"

    def __init__(self, user_id: str = DEFAULT_USER):
        self.user_id = user_id
        self.intent = self._load()

    def _filepath(self) -> str:
        dir_path = os.path.join(get_data_path(), "intent")
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, f"{self.user_id}.json")

    def _load(self) -> UserIntent:
        fp = self._filepath()
        if not os.path.exists(fp):
            return UserIntent()
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            intent = UserIntent(**data)
            intent.DECAY_FACTOR = data.get('DECAY_FACTOR', 0.95)
            intent.MAX_RECENT = data.get('MAX_RECENT', 20)
            return intent
        except Exception:
            return UserIntent()

    def _save(self):
        self.intent.last_updated = time.time()
        fp = self._filepath()
        try:
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.intent), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def update(self, query: str, result: str,
               sectors: List[str] = None,
               stocks: List[str] = None,
               question_type: str = None):
        """
        根据一次查询更新意图画像

        更新策略：
          1. 衰减：所有现有权重乘以 DECAY_FACTOR（< 0.1 的删除）
          2. 增强：本次涉及的板块/个股 +1.0（上限 10.0）
          3. 记录：追加到 recent_queries（最多 MAX_RECENT 条）
          4. 推断：根据 question_types 推断 preferred_depth
        """
        now = time.time()

        for key in list(self.intent.watched_sectors.keys()):
            self.intent.watched_sectors[key] *= self.intent.DECAY_FACTOR
            if self.intent.watched_sectors[key] < 0.1:
                del self.intent.watched_sectors[key]

        for key in list(self.intent.watched_stocks.keys()):
            self.intent.watched_stocks[key] *= self.intent.DECAY_FACTOR
            if self.intent.watched_stocks[key] < 0.1:
                del self.intent.watched_stocks[key]

        for sector in (sectors or []):
            self.intent.watched_sectors[sector] = min(
                self.intent.watched_sectors.get(sector, 0) + 1.0, 10.0
            )

        for stock in (stocks or []):
            self.intent.watched_stocks[stock] = min(
                self.intent.watched_stocks.get(stock, 0) + 1.0, 10.0
            )

        if question_type:
            self.intent.question_types[question_type] = \
                self.intent.question_types.get(question_type, 0) + 1

        self.intent.recent_queries.append({
            "query": query,
            "summary": (result or "")[:200],
            "time": now,
        })
        if len(self.intent.recent_queries) > self.intent.MAX_RECENT:
            self.intent.recent_queries = self.intent.recent_queries[-self.intent.MAX_RECENT:]

        type_counts = self.intent.question_types
        if type_counts.get("discussion", 0) > type_counts.get("analysis", 0):
            self.intent.preferred_depth = "simple"
        elif type_counts.get("analysis", 0) > type_counts.get("query", 0):
            self.intent.preferred_depth = "deep"
        else:
            self.intent.preferred_depth = "medium"

        self._save()

    def extract_entities(self, text: str) -> tuple:
        """
        从文本中提取板块和个股

        :return: (sectors: list, stocks: list)
        """
        sectors = self._extract_sectors_regex(text)
        stocks = self._extract_stocks_regex(text, sectors)
        return sectors, stocks

    def _extract_sectors_regex(self, text: str) -> list:
        """基于词库提取板块名"""
        found = []
        for sector in KNOWN_SECTORS:
            if sector in text:
                found.append(sector)
        return found

    def _extract_stocks_regex(self, text: str, exclude_sectors: list) -> list:
        """
        基于后缀词库提取个股名

        排除逻辑：
          - 已识别的板块名不作为个股（如"锂电池板块"中的"锂电"是板块）
          - 名称长度 < 2 的不匹配
          - 不重复提取
        """
        if not text:
            return []

        pattern = '|'.join(re.escape(s) for s in KNOWN_SUFFIXES)
        stock_pattern = re.compile(
            rf'([\u4e00-\u9fa5]{{2,6}})(?:{pattern})'
        )
        candidates = stock_pattern.findall(text)

        filtered = []
        for name in candidates:
            if len(name) < 2:
                continue
            if name in exclude_sectors:
                continue
            if name not in filtered:
                filtered.append(name)
        return filtered

    def get_context_hint(self) -> str:
        """
        生成上下文提示，注入到 Agent 的 system prompt 中
        """
        hints = []

        if self.intent.watched_sectors:
            top = sorted(
                self.intent.watched_sectors.items(),
                key=lambda x: x[1], reverse=True
            )[:3]
            hints.append(f"用户近期关注板块：{'、'.join(s for s, _ in top)}")

        if self.intent.watched_stocks:
            top = sorted(
                self.intent.watched_stocks.items(),
                key=lambda x: x[1], reverse=True
            )[:5]
            hints.append(f"用户近期关注个股：{'、'.join(s for s, _ in top)}")

        depth_map = {
            "simple": "用户偏好简洁的讨论式回答",
            "medium": "用户偏好标准的分析报告",
            "deep": "用户偏好详细的多维度分析",
        }
        if self.intent.preferred_depth in depth_map:
            hints.append(depth_map[self.intent.preferred_depth])

        if self.intent.recent_queries:
            last = self.intent.recent_queries[-1]
            hints.append(f"用户上一次查询：{last['query']}")

        if not hints:
            return ""

        return "## 用户画像（供参考）\n" + "\n".join(f"- {h}" for h in hints)
