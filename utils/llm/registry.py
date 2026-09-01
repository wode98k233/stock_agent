"""
选股雷达 — LLM 模型注册表
=========================

职责：**把「哪个用途用哪个模型、降级顺序、限流多少」这一份配置，集中到一个地方**。

配置来源（优先级从低到高）：
  1. ``config.Config`` 现有 env（OPENAI_* / REPORT_LLM_* / COMPRESS_LLM_* / JUDGE_LLM_* / STOCK_MEMORY_*）
     —— 老配置完全兼容，不废。
  2. 可选 ``models.yaml`` / ``models.json``（由 ``LLM_GATEWAY_MODELS_PATH`` 指定，或同目录默认文件）
     —— 只覆盖「模型名 + 降级链 + 限流」，凭证仍从 env 读（安全：不把 key 写进配置文件）。

设计要点：
- 每个 chat 用途在 ``_ENV_MAP`` 里对应一组 env 凭证键；未配置的用途回退到 agent 默认。
- embed/rerank 从 ``STOCK_MEMORY_*`` 读，provider 由 ``STOCK_MEMORY_EMBEDDING`` /
  ``STOCK_MEMORY_RERANKER`` 的值决定。

扩展指引（详见设计规格 §7）：
- 新增 chat 用途：在 ``types.Purpose`` 加成员 → 在 ``_ENV_MAP`` 加 env 键 → 可选在 yaml 配模型。
- 改模型/降级/限流：改 yaml 即可，无需动代码。
"""

import json
import os
from dataclasses import replace
from typing import Optional

from utils.llm.types import (
    Purpose,
    ModelProfile,
    RateLimitConfig,
    ChatModelSpec,
    EmbedSpec,
    RerankSpec,
)
from config import Config


# chat 用途 → (model 键, api_key 键, base_url 键)
# 未列出的用途回退到 agent 的 OPENAI_*（与旧 get_*_llm 行为一致）。
_ENV_MAP = {
    Purpose.AGENT: ("OPENAI_MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE"),
    Purpose.REPORT: ("REPORT_LLM_MODEL", "REPORT_LLM_API_KEY", "REPORT_LLM_API_BASE"),
    Purpose.COMPRESS: ("COMPRESS_LLM_MODEL", "COMPRESS_LLM_API_KEY", "COMPRESS_LLM_API_BASE"),
    Purpose.JUDGE: ("JUDGE_LLM_MODEL", "JUDGE_LLM_API_KEY", "JUDGE_LLM_API_BASE"),
}

# 每个用途的默认限流（0 = 不限）。yaml 的 limits.<purpose> 可覆盖。
_DEFAULT_LIMITS = {
    Purpose.AGENT: RateLimitConfig(rpm=60, tpm=200_000),
    Purpose.REPORT: RateLimitConfig(rpm=20, tpm=80_000),
    Purpose.COMPRESS: RateLimitConfig(rpm=120, tpm=100_000),
    Purpose.JUDGE: RateLimitConfig(rpm=30, tpm=60_000),
    Purpose.MEMORY: RateLimitConfig(rpm=60, tpm=120_000),
}


def _default_yaml_path() -> str:
    env_path = os.getenv("LLM_GATEWAY_MODELS_PATH", "")
    if env_path:
        return env_path
    return os.path.join(os.path.dirname(__file__), "models.yaml")


class ModelRegistry:
    """从 env（+ 可选 yaml/json）解析各用途的模型规格。"""

    def __init__(self, yaml_path: Optional[str] = None):
        self._yaml_path = yaml_path or _default_yaml_path()
        self._yaml = self._load_override(self._yaml_path)

    # ── 覆盖文件加载（可选） ──

    def _load_override(self, path: str) -> Optional[dict]:
        """加载可选覆盖文件。

        支持 .yaml（需 PyYAML）或 .json。文件不存在 / 解析失败都返回 None，
        不影响 env 默认配置——保证「没配文件也能跑」。
        """
        if not path or not os.path.isfile(path):
            return None
        try:
            if path.endswith(".json"):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            try:
                import yaml  # type: ignore
            except ImportError:
                return None
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            return None

    # ── chat 规格 ──

    def _profile_for(self, purpose: Purpose) -> ModelProfile:
        """按用途读取 env 凭证，构造 ModelProfile。未配置则回退 agent 默认。"""
        model_attr, key_attr, base_attr = _ENV_MAP.get(purpose, _ENV_MAP[Purpose.AGENT])
        model = getattr(Config, model_attr, "") or ""
        if not model:
            # 与旧 get_*_llm 一致：该用途没单独配置时，用 agent 主配置
            return ModelProfile(
                model=Config.OPENAI_MODEL_NAME,
                api_key=Config.OPENAI_API_KEY,
                base_url=Config.OPENAI_API_BASE,
            )
        api_key = getattr(Config, key_attr, "") or Config.OPENAI_API_KEY
        base_url = getattr(Config, base_attr, "") or Config.OPENAI_API_BASE
        return ModelProfile(model=model, api_key=api_key, base_url=base_url)

    def _limits_for(self, purpose: Purpose) -> RateLimitConfig:
        if self._yaml:
            raw = self._yaml.get("limits", {}).get(purpose.value)
            if isinstance(raw, dict):
                return RateLimitConfig(
                    rpm=int(raw.get("rpm", 0) or 0),
                    tpm=int(raw.get("tpm", 0) or 0),
                )
        return _DEFAULT_LIMITS.get(purpose, RateLimitConfig())

    def get_chat_spec(self, purpose) -> ChatModelSpec:
        """返回某 chat 用途的规格（主模型 + 降级链 + 限流）。

        未知 purpose 会被 Purpose.coerce 静默回退到 agent，不抛异常。
        """
        purpose = Purpose.coerce(purpose)
        base = self._profile_for(purpose)
        limits = self._limits_for(purpose)

        # 1) yaml 覆盖：模型名 + 降级链 + 限流
        yaml_chat = self._yaml.get("chat", {}).get(purpose.value) if self._yaml else None
        if yaml_chat and isinstance(yaml_chat, dict):
            primary_model = (yaml_chat.get("primary") or {}).get("model") or base.model
            primary = replace(base, model=primary_model) if primary_model else base
            fb_models = yaml_chat.get("fallbacks", []) or []
            fallbacks = [replace(base, model=m) for m in fb_models if m]
            if "limits" in yaml_chat and isinstance(yaml_chat["limits"], dict):
                limits = RateLimitConfig(
                    rpm=int(yaml_chat["limits"].get("rpm", 0) or 0),
                    tpm=int(yaml_chat["limits"].get("tpm", 0) or 0),
                )
            return ChatModelSpec(purpose=purpose, primary=primary, fallbacks=fallbacks, limits=limits)

        # 2) 纯 env
        return ChatModelSpec(purpose=purpose, primary=base, fallbacks=[], limits=limits)

    # ── embedding 规格 ──

    def get_embed_spec(self) -> EmbedSpec:
        emb_type = (getattr(Config, "STOCK_MEMORY_EMBEDDING", "") or "").strip().lower()
        # 归一化：remote 等同 openai 兼容协议
        if emb_type in ("openai", "remote"):
            provider = "openai"
        elif emb_type == "ollama":
            provider = "ollama"
        else:
            provider = "local"  # 含空值：默认本地
        return EmbedSpec(
            purpose=Purpose.MEMORY,
            provider=provider,
            model=getattr(Config, "STOCK_MEMORY_EMBEDDING_MODEL", "") or "",
            api_key=getattr(Config, "STOCK_MEMORY_API_KEY", "") or Config.OPENAI_API_KEY,
            base_url=getattr(Config, "STOCK_MEMORY_API_BASE", "") or Config.OPENAI_API_BASE,
            local_model=getattr(Config, "STOCK_MEMORY_LOCAL_MODEL", "") or "",
            limits=self._limits_for(Purpose.MEMORY),
        )

    # ── rerank 规格 ──

    def get_rerank_spec(self) -> RerankSpec:
        rerank_type = (getattr(Config, "STOCK_MEMORY_RERANKER", "") or "").strip().lower()
        provider = "openai" if rerank_type == "openai" else "local"
        return RerankSpec(
            purpose=Purpose.MEMORY,
            provider=provider,
            model=getattr(Config, "STOCK_MEMORY_RERANKER_MODEL", "") or "",
            api_key=getattr(Config, "STOCK_MEMORY_API_KEY", "") or Config.OPENAI_API_KEY,
            base_url=getattr(Config, "STOCK_MEMORY_API_BASE", "") or Config.OPENAI_API_BASE,
            local_dir=getattr(Config, "STOCK_MEMORY_RERANKER_LOCAL_DIR", "") or "",
            limits=self._limits_for(Purpose.MEMORY),
        )
