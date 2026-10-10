"""
选股雷达 — LLM 网关模型路由策略（A8 T04）
==========================================

**解决什么问题**：「查一下 600519 现价」和「深度研究牧原」用的是同一档模型——
简单查询为强模型付费，深度任务被弱模型限质。本 policy 按调用方传入的 ``sr_scenario``
（classifier 输出）+ ``label`` + ``purpose``，经**配置化映射表**解析出 fast/standard/strong
三档，网关据此只改 primary 模型名（凭证沿用 purpose 原配置，降级链不变）。

设计裁决（docs/arch/A8-语义缓存与模型路由-设计.md §1.5）：
- 映射表进配置 ``LLM_ROUTING_MAP``（JSON），**每次 resolve 现读 Config → 热改生效**（FR-05）；
- 规则按顺序首条命中生效；``match`` 支持 scenario / label / purpose 三种维度（可组合，
  同一规则多维度为「与」关系）；未命中走 ``default``（默认 standard）；
- 档→模型绑定走 env：``LLM_TIER_FAST_MODEL`` / ``LLM_TIER_STANDARD_MODEL``（空=purpose 原配）/
  ``LLM_TIER_STRONG_MODEL``；不新增 provider；
- 路由计数落共享 ``CacheStats``（与缓存统计同源，供 stats 接口聚合）。

扩展指引：要加档位/改默认规则，改 ``VALID_TIERS`` / ``DEFAULT_ROUTING_MAP`` 或直接调配置。
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from utils.llm.policies.cache_policy import get_shared_stats

_log = logging.getLogger("utils.llm.policies.routing_policy")

VALID_TIERS = ("fast", "standard", "strong")  # 三档（设计 §1.5）
DEFAULT_TIER = "standard"

# 默认路由映射（可用 LLM_ROUTING_MAP 整体覆盖）
DEFAULT_ROUTING_MAP = {
    "rules": [
        {"match": {"scenario": ["market_quick", "data_query"]}, "tier": "fast"},
        {"match": {"label": ["report_enhance", "deep_research", "debate"]}, "tier": "strong"},
        {"match": {"purpose": ["compress", "judge"]}, "tier": "fast"},
    ],
    "default": "standard",
}


def _norm_tier(value) -> str:
    """档位归一化：非法值静默回退 standard（配置失误不崩网关）。"""
    s = str(value or "").strip().lower()
    return s if s in VALID_TIERS else DEFAULT_TIER


def _load_routing_config() -> dict:
    """读 Config.LLM_ROUTING_MAP；解析失败回退默认表（fail-safe）。"""
    try:
        from config import Config
        raw = getattr(Config, "LLM_ROUTING_MAP", "")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        _log.warning("配置 LLM_ROUTING_MAP 解析失败，使用默认路由表: %s", e)
    return dict(DEFAULT_ROUTING_MAP)


@dataclass
class RoutingDecision:
    """路由决策结果。

    :param tier: 命中档位（fast/standard/strong）
    :param model_override: 该档绑定的模型名；None = 沿用 purpose 原配模型
    :param reason: 命中规则描述（写 trace / 日志用）
    """

    tier: str = DEFAULT_TIER
    model_override: Optional[str] = None
    reason: str = ""


class ModelRoutingPolicy:
    """模型分档路由策略（配置化映射表，热改生效）。

    :param routing_map: 显式映射表（测试用）；留空每次 resolve 读 Config（热改生效）。
    :param tier_models: 显式档→模型绑定（测试用）；留空读 Config.LLM_TIER_*_MODEL。
    :param enabled: 显式开关（测试用）；留空读 Config.LLM_ROUTING_ENABLED（默认 false 灰度）。
    """

    def __init__(self, routing_map: Optional[dict] = None,
                 tier_models: Optional[dict] = None,
                 enabled: Optional[bool] = None):
        self._routing_map_override = routing_map
        self._tier_models_override = dict(tier_models) if tier_models else None
        self._enabled_override = enabled

    # ── 配置解析 ──

    @property
    def enabled(self) -> bool:
        """路由总开关（默认 false 灰度；关闭后网关不调 resolve，行为与上线前一致，AC-6）。"""
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        try:
            from config import Config
            return bool(getattr(Config, "LLM_ROUTING_ENABLED", False))
        except Exception:
            return False

    def get_routing_map(self) -> dict:
        if self._routing_map_override is not None:
            return self._routing_map_override
        return _load_routing_config()

    def get_tier_models(self) -> dict:
        if self._tier_models_override is not None:
            return dict(self._tier_models_override)
        try:
            from config import Config
            return {
                "fast": getattr(Config, "LLM_TIER_FAST_MODEL", "") or "",
                "standard": getattr(Config, "LLM_TIER_STANDARD_MODEL", "") or "",
                "strong": getattr(Config, "LLM_TIER_STRONG_MODEL", "") or "",
            }
        except Exception:
            return {"fast": "", "standard": "", "strong": ""}

    # ── 核心解析 ──

    def resolve(self, *, purpose: str, label: str,
                metadata: Optional[dict]) -> RoutingDecision:
        """解析 (purpose, label, sr_scenario) → RoutingDecision。

        规则按顺序首条命中生效；``match`` 多维度为「与」关系；未命中走 default。
        开关关闭时返回 standard / 无 override（调用方一般不会走到这里，仍保证安全）。
        """
        if not self.enabled:
            return RoutingDecision(tier=DEFAULT_TIER, model_override=None, reason="routing_disabled")
        rmap = self.get_routing_map()
        rules = rmap.get("rules") if isinstance(rmap, dict) else None
        meta = metadata or {}
        scenario = str(meta.get("sr_scenario") or "")
        if isinstance(rules, list):
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    continue
                match = rule.get("match") or {}
                if not isinstance(match, dict):
                    continue
                # 多维度「与」：声明了的维度都必须命中
                if "scenario" in match and scenario not in (match.get("scenario") or []):
                    continue
                if "label" in match and str(label or "") not in (match.get("label") or []):
                    continue
                if "purpose" in match and str(purpose or "") not in (match.get("purpose") or []):
                    continue
                tier = _norm_tier(rule.get("tier"))
                decision = RoutingDecision(
                    tier=tier, model_override=self._model_for(tier), reason=f"rule#{i}"
                )
                self.record_tier(decision.tier)  # 档位计数与缓存统计同源（stats 接口聚合）
                return decision
        tier = _norm_tier(rmap.get("default", DEFAULT_TIER)) if isinstance(rmap, dict) else DEFAULT_TIER
        decision = RoutingDecision(tier=tier, model_override=self._model_for(tier), reason="default")
        self.record_tier(decision.tier)
        return decision

    def _model_for(self, tier: str) -> Optional[str]:
        """档→模型绑定；空字符串视为未绑定（返回 None = 沿用 purpose 原配）。"""
        name = str((self.get_tier_models().get(tier) or "")).strip()
        return name or None

    # ── 计量 ──

    @staticmethod
    def record_tier(tier: str) -> None:
        """路由档位计数（与缓存统计同源，供 stats 接口聚合展示）。"""
        get_shared_stats().record_routing(tier)
