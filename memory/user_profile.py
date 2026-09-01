"""
记忆系统 — 用户画像管理
增强 intent.py：从浅层权重升级为结构化用户画像，同时保留原有衰减式权重追踪。

与 intent.py 的关系：
  - user_profile.py 提供 IntentMemoryManager 的超集功能
  - 新增结构化字段（风险偏好、持仓、反馈等）
  - intent.py 保持不变，现有代码不受影响，逐步迁移
"""
import json
import os
import time
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from utils.app_paths import get_data_path, get_user_profile_dir

# ── 实体提取词库（沿用 intent.py 的词库） ──────────────────────
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


# ── 数据模型 ────────────────────────────────────────────────
@dataclass
class Holding:
    """持仓信息"""
    stock_code: str
    stock_name: str
    cost: float = 0.0           # 成本价
    shares: int = 0             # 持股数量
    added_date: str = ""        # 添加日期


@dataclass
class Feedback:
    """用户反馈记录"""
    date: str
    query: str
    rating: str = ""            # "good" | "neutral" | "bad"
    comment: str = ""


@dataclass
class UserProfile:
    """用户画像——结构化持久化"""

    # === 继承自 intent.py 的字段 ===
    watched_sectors: Dict[str, float] = field(default_factory=dict)
    watched_stocks: Dict[str, float] = field(default_factory=dict)
    question_types: Dict[str, int] = field(default_factory=dict)
    preferred_depth: str = "medium"
    recent_queries: List[dict] = field(default_factory=list)

    # === 新增：显式偏好（用户主动设置或从对话隐式提取） ===
    risk_tolerance: str = ""            # "conservative" | "moderate" | "aggressive"
    investment_horizon: str = ""        # "short" | "medium" | "long"
    preferred_indicators: List[str] = field(default_factory=list)  # ["MACD","PE","ROE"]
    output_detail: str = "standard"     # "brief" | "standard" | "detailed"
    analysis_style: str = ""            # "technical_heavy" | "fundamental_heavy" | "balanced"

    # === 新增：持仓 ===
    holdings: List[Holding] = field(default_factory=list)

    # === 新增：反馈历史 ===
    feedback_history: List[Feedback] = field(default_factory=list)

    # === 元数据 ===
    last_updated: float = 0.0
    DECAY_FACTOR: float = 0.95
    MAX_RECENT: int = 20
    MAX_FEEDBACK: int = 50


# ── 管理器 ──────────────────────────────────────────────────
class UserProfileManager:
    """
    用户画像管理器

    存储位置：
      - JSON 快照：data/user_profile/{user_id}.json（完整画像，低频读写）
      - SQLite 统计：stock_memory.db.user_profile_stats（高频计数字段）

    与 IntentMemoryManager 的兼容：
      - update() / extract_entities() / get_context_hint() 签名完全一致
      - 可无缝替换 intent.py 中的 IntentMemoryManager
    """

    DEFAULT_USER = "default"

    def __init__(self, user_id: str = DEFAULT_USER,
                 db_path: Optional[str] = None,
                 profile_dir: Optional[str] = None):
        self.user_id = user_id
        self._db_path = db_path
        self._profile_dir = profile_dir or get_user_profile_dir()
        self.profile = self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _filepath(self) -> str:
        return os.path.join(self._profile_dir, f"{self.user_id}.json")

    def _load(self) -> UserProfile:
        fp = self._filepath()
        if not os.path.exists(fp):
            return UserProfile()
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            profile = UserProfile()
            # 简单字段
            for key in ("preferred_depth", "risk_tolerance", "investment_horizon",
                        "output_detail", "analysis_style", "last_updated",
                        "DECAY_FACTOR", "MAX_RECENT", "MAX_FEEDBACK"):
                if key in data:
                    setattr(profile, key, data[key])
            # Dict 字段
            for key in ("watched_sectors", "watched_stocks", "question_types"):
                if key in data and isinstance(data[key], dict):
                    setattr(profile, key, data[key])
            # List 字段
            if "recent_queries" in data:
                profile.recent_queries = data["recent_queries"]
            if "preferred_indicators" in data:
                profile.preferred_indicators = data["preferred_indicators"]
            # 嵌套对象字段
            if "holdings" in data:
                profile.holdings = [Holding(**h) for h in data["holdings"]]
            if "feedback_history" in data:
                profile.feedback_history = [Feedback(**f) for f in data["feedback_history"]]
            return profile
        except Exception:
            return UserProfile()

    def _save(self):
        self.profile.last_updated = time.time()
        fp = self._filepath()
        os.makedirs(self._profile_dir, exist_ok=True)
        try:
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.profile), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── 意图追踪（兼容 intent.py API） ────────────────────

    def update(self, query: str, result: str,
               sectors: List[str] = None,
               stocks: List[str] = None,
               question_type: str = None):
        """
        根据一次查询更新画像权重（衰减 + 增强）
        签名与 IntentMemoryManager.update() 完全一致
        """
        p = self.profile

        # 衰减
        for key in list(p.watched_sectors.keys()):
            p.watched_sectors[key] *= p.DECAY_FACTOR
            if p.watched_sectors[key] < 0.1:
                del p.watched_sectors[key]

        for key in list(p.watched_stocks.keys()):
            p.watched_stocks[key] *= p.DECAY_FACTOR
            if p.watched_stocks[key] < 0.1:
                del p.watched_stocks[key]

        # 增强
        for sector in (sectors or []):
            p.watched_sectors[sector] = min(
                p.watched_sectors.get(sector, 0) + 1.0, 10.0
            )

        for stock in (stocks or []):
            p.watched_stocks[stock] = min(
                p.watched_stocks.get(stock, 0) + 1.0, 10.0
            )

        if question_type:
            p.question_types[question_type] = \
                p.question_types.get(question_type, 0) + 1

        # 最近查询
        p.recent_queries.append({
            "query": query,
            "summary": (result or "")[:200],
            "time": time.time(),
        })
        if len(p.recent_queries) > p.MAX_RECENT:
            p.recent_queries = p.recent_queries[-p.MAX_RECENT:]

        # 推断深度偏好
        type_counts = p.question_types
        if type_counts.get("discussion", 0) > type_counts.get("analysis", 0):
            p.preferred_depth = "simple"
        elif type_counts.get("analysis", 0) > type_counts.get("query", 0):
            p.preferred_depth = "deep"
        else:
            p.preferred_depth = "medium"

        self._save()

    def extract_entities(self, text: str) -> tuple:
        """从文本中提取板块和个股（与 intent.py 完全一致）"""
        sectors = self._extract_sectors_regex(text)
        stocks = self._extract_stocks_regex(text, sectors)
        return sectors, stocks

    def _extract_sectors_regex(self, text: str) -> list:
        found = []
        for sector in KNOWN_SECTORS:
            if sector in text:
                found.append(sector)
        return found

    def _extract_stocks_regex(self, text: str, exclude_sectors: list) -> list:
        if not text:
            return []
        pattern = '|'.join(re.escape(s) for s in KNOWN_SUFFIXES)
        stock_pattern = re.compile(rf'([一-龥]{{2,6}})(?:{pattern})')
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

    # ── 上下文提示（增强版） ──────────────────────────────

    def get_context_hint(self) -> str:
        """生成上下文提示——增强版，包含结构化画像信息"""
        hints = []
        p = self.profile

        # 持仓
        if p.holdings:
            holding_str = "、".join(
                f"{h.stock_name}({h.stock_code})" for h in p.holdings[:5]
            )
            hints.append(f"用户持仓：{holding_str}")

        # 风险偏好
        risk_map = {
            "conservative": "用户风险偏好：稳健型，关注估值安全边际和回撤控制",
            "moderate": "用户风险偏好：平衡型，兼顾成长性与估值",
            "aggressive": "用户风险偏好：激进型，愿意承担较高波动追求收益",
        }
        if p.risk_tolerance in risk_map:
            hints.append(risk_map[p.risk_tolerance])

        # 投资周期
        horizon_map = {
            "short": "用户投资周期：短线（数天到数周）",
            "medium": "用户投资周期：中线（数月）",
            "long": "用户投资周期：长线（半年以上）",
        }
        if p.investment_horizon in horizon_map:
            hints.append(horizon_map[p.investment_horizon])

        # 关注指标
        if p.preferred_indicators:
            hints.append(f"用户关注指标：{'、'.join(p.preferred_indicators)}")

        # 关注板块
        if p.watched_sectors:
            top = sorted(p.watched_sectors.items(), key=lambda x: x[1], reverse=True)[:3]
            hints.append(f"用户近期关注板块：{'、'.join(s for s, _ in top)}")

        # 关注个股
        if p.watched_stocks:
            top = sorted(p.watched_stocks.items(), key=lambda x: x[1], reverse=True)[:5]
            hints.append(f"用户近期关注个股：{'、'.join(s for s, _ in top)}")

        # 分析风格
        style_map = {
            "technical_heavy": "用户偏好以技术面为主的分析",
            "fundamental_heavy": "用户偏好以基本面为主的分析",
            "balanced": "用户偏好技术面+基本面均衡的分析",
        }
        if p.analysis_style in style_map:
            hints.append(style_map[p.analysis_style])

        # 上次查询（带日期，避免 LLM 误判为当天提问）
        if p.recent_queries:
            from datetime import datetime as dt
            last = p.recent_queries[-1]
            when = ""
            ts = last.get("time", 0)
            if ts:
                when = dt.fromtimestamp(ts).strftime("%m-%d %H:%M") + " — "
            hints.append(f"用户上一次查询（{when}）：{last['query']}")

        if not hints:
            return ""

        return "\n## 用户画像\n" + "\n".join(f"- {h}" for h in hints)

    # ── 新增：结构化画像操作 ──────────────────────────────

    def set_risk_tolerance(self, level: str):
        """设置风险偏好"""
        if level in ("conservative", "moderate", "aggressive"):
            self.profile.risk_tolerance = level
            self._save()

    def set_investment_horizon(self, horizon: str):
        if horizon in ("short", "medium", "long"):
            self.profile.investment_horizon = horizon
            self._save()

    def set_analysis_style(self, style: str):
        if style in ("technical_heavy", "fundamental_heavy", "balanced"):
            self.profile.analysis_style = style
            self._save()

    def add_holding(self, stock_code: str, stock_name: str,
                    cost: float = 0.0, shares: int = 0):
        """添加或更新持仓"""
        for h in self.profile.holdings:
            if h.stock_code == stock_code:
                h.cost = cost
                h.shares = shares
                self._save()
                return
        self.profile.holdings.append(Holding(
            stock_code=stock_code,
            stock_name=stock_name,
            cost=cost,
            shares=shares,
            added_date=time.strftime("%Y-%m-%d"),
        ))
        self._save()

    def remove_holding(self, stock_code: str):
        self.profile.holdings = [
            h for h in self.profile.holdings if h.stock_code != stock_code
        ]
        self._save()

    def record_feedback(self, query: str, rating: str, comment: str = ""):
        """记录用户反馈"""
        self.profile.feedback_history.append(Feedback(
            date=time.strftime("%Y-%m-%d %H:%M"),
            query=query,
            rating=rating,
            comment=comment,
        ))
        if len(self.profile.feedback_history) > self.profile.MAX_FEEDBACK:
            self.profile.feedback_history = \
                self.profile.feedback_history[-self.profile.MAX_FEEDBACK:]
        self._save()

    def extract_from_conversation(self, user_text: str):
        """
        从用户对话中隐式提取偏好。
        示例：
          "我比较保守" → risk_tolerance = "conservative"
          "我是做短线的" → investment_horizon = "short"
          "我更喜欢看技术指标" → analysis_style = "technical_heavy"
        """
        text = user_text.lower()

        if any(w in text for w in ["保守", "稳健", "安全", "低风险", "回撤", "怕亏"]):
            if self.profile.risk_tolerance != "conservative":
                self.profile.risk_tolerance = "conservative"
                self._save()

        if any(w in text for w in ["激进", "高风险", "敢赌", "不怕波动", "弹性"]):
            if self.profile.risk_tolerance != "aggressive":
                self.profile.risk_tolerance = "aggressive"
                self._save()

        if any(w in text for w in ["短线", "快进快出", "做t", "打板", "超短"]):
            if self.profile.investment_horizon != "short":
                self.profile.investment_horizon = "short"
                self._save()

        if any(w in text for w in ["长线", "价值投资", "长期持有", "定投", "拿住"]):
            if self.profile.investment_horizon != "long":
                self.profile.investment_horizon = "long"
                self._save()

        if any(w in text for w in ["技术指标", "k线", "macd", "均线", "布林带", "技术面"]):
            if self.profile.analysis_style != "technical_heavy":
                self.profile.analysis_style = "technical_heavy"
                self._save()

        if any(w in text for w in ["基本面", "财报", "估值", "roe", "pe", "现金流", "护城河"]):
            if self.profile.analysis_style != "fundamental_heavy":
                self.profile.analysis_style = "fundamental_heavy"
                self._save()

        # 持仓提取
        if "持有" in text or "买了" in text or "持仓" in text:
            # 尝试提取 "持有贵州茅台" / "买了宁德时代"
            for stock_name in re.findall(r'[一-龥]{2,6}', text):
                if any(suffix in stock_name for suffix in KNOWN_SUFFIXES[:5]):
                    if not any(h.stock_name == stock_name for h in self.profile.holdings):
                        # 无法获取代码，先用名称记录
                        pass  # 持仓添加需要代码，此处仅标记潜力
