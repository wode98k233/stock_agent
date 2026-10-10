"""用户画像的语义抽取（P2，2026-09-23）。

关键词/正则（`UserProfileManager.extract_from_conversation`）覆盖常见表述，但漏新词/同义
表达（如「我偏向稳一点」）。本模块用 **prototype 向量相似度** 做补充：把每个偏好取值写成
一句「种子语」，用户文本与各种子语比余弦，取最接近且显著领先者。

设计约束：
- **懒加载 + 进程内缓存** 种子向量（每个 label 只 embed 一次）；
- 有**廉价前置门**（cue 词），无可疑偏好表述时直接跳过，避免无谓 embedding；
- 任一步失败都静默返回空（不阻塞主流程，尊重免费额度）。
"""
import logging
import re
from typing import Callable, Optional

_log = logging.getLogger(__name__)

# 每个偏好取值一句「种子语」（含典型表述与关键词，便于语义/词面双匹配）
_PROTOTYPES = {
    "risk_tolerance": {
        "conservative": "我比较保守稳健，注重回撤控制，怕亏损，追求安全边际，不追高",
        "moderate": "我风险偏好适中，平衡型，兼顾成长性与估值，能接受一定波动",
        "aggressive": "我比较激进，愿意承担高波动追求收益，敢赌，追求高弹性",
    },
    "investment_horizon": {
        "short": "我是短线交易，快进快出，做T打板，持股几天到几周",
        "medium": "我是中线持股，波段操作，持有几个月",
        "long": "我是长线价值投资，长期持有，定投，拿住半年以上",
    },
    "analysis_style": {
        "technical_heavy": "我主要看技术指标，K线均线MACD，以技术面为主",
        "fundamental_heavy": "我主要看基本面，财报估值现金流，以基本面为主",
        "balanced": "我技术面和基本面均衡，两者都要看",
    },
}

# 廉价前置门：没有这些 cue 时，用户大概率在表达偏好
_CUE_RE = re.compile(
    "保守|稳健|激进|风险|回撤|波动|怕亏|安全边际|"
    "短线|长线|中线|波段|打板|快进快出|定投|长期持有|"
    "技术|基本面|估值|指标|K线|均线|财报|现金流|"
    "偏好|风格|习惯|仓位|配置"
)

# 进程内缓存：id(embedding_fn) -> {(field,label): vector}
_VEC_CACHE: dict = {}


def _cos(a, b) -> float:
    import math
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return s / (na * nb) if na and nb else 0.0


def _proto_vec(cache: dict, embedding_fn: Callable, field: str, label: str,
               text: str) -> Optional[list]:
    key = (field, label)
    if key in cache:
        return cache[key]
    try:
        vec = embedding_fn(text)
    except Exception:
        return None
    cache[key] = vec
    return vec


def extract_preferences(text: str, embedding_fn: Optional[Callable],
                        threshold: float = 0.5, margin: float = 0.02) -> dict:
    """从用户文本语义抽取偏好，返回 ``{field: label}``（不确定的字段不返回）。

    Args:
        embedding_fn: 单文本 → 向量的可调用对象；None 直接返回空。
        threshold: 最高分需 ≥ 此值（余弦，bge-m3 相关文本约 0.5~0.7）。
        margin: 最高分需比**次优 label** 高出此值（避免歧义）。
    """
    if not text or not callable(embedding_fn):
        return {}
    text = text.strip()
    if not text or not _CUE_RE.search(text):
        return {}
    cache = _VEC_CACHE.setdefault(id(embedding_fn), {})
    try:
        tv = embedding_fn(text)
    except Exception:
        return {}
    if not tv:
        return {}

    out: dict = {}
    for field, protos in _PROTOTYPES.items():
        label_scores = {}
        for label, proto_text in protos.items():
            pv = _proto_vec(cache, embedding_fn, field, label, proto_text)
            if pv is None:
                continue
            label_scores[label] = _cos(tv, pv)
        if not label_scores:
            continue
        ranked = sorted(label_scores.items(), key=lambda kv: kv[1], reverse=True)
        best_label, best_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if best_score >= threshold and (best_score - second) >= margin:
            out[field] = best_label
    return out
