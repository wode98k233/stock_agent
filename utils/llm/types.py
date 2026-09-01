"""
选股雷达 — LLM 网关类型契约
================================

本模块只定义「数据形状」，不含任何逻辑。网关其它模块都依赖这里的定义。

设计原则（详见 docs/superpowers/specs/2026-07-12-llm-gateway-design.md）：
- **Purpose（用途）** 是网关的一等公民。加一种用途 = 在 `Purpose` 加一个枚举成员
  + 在 models.yaml（或 env）配一段，调用点零改动。
- **ModelProfile** 描述「单个模型怎么连」；**ChatModelSpec** 描述「一个用途用什么模型、
  降级顺序、限流多少」。embed/rerank 各有自己的 Spec。

扩展指引：
- 新增 chat 用途：在 `Purpose` 加成员，registry 自动按 `_ENV_MAP` 找凭证，无需改网关代码。
- 新增字段：在对应 dataclass 加字段即可，调用链（gateway）按字段读取，未用到的字段无害。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Purpose(str, Enum):
    """LLM 调用用途。

    新增用途的步骤（零侵入）：
      1. 在这里加一个成员，例如 ``SUMMARIZE = "summarize"``；
      2. 在 registry 的 ``_ENV_MAP`` 里给它配 env 凭证键（或用 agent 默认）；
      3. 在 models.yaml 的 ``chat.summarize`` 配模型与降级链（可选）；
      4. 调用方 ``gateway.chat("summarize")`` 即可。
    """

    AGENT = "agent"      # 主 Agent 推理（默认）
    REPORT = "report"    # 报告生成
    COMPRESS = "compress"  # 上下文压缩
    JUDGE = "judge"      # 评测 Judge
    MEMORY = "memory"    # 记忆系统的 embedding / reranker

    @classmethod
    def coerce(cls, val) -> "Purpose":
        """接受枚举或字符串，统一成 Purpose。

        未知值（拼写错误或尚未配置）**静默回退 AGENT**，避免一次配置失误就崩整个调用。
        """
        if isinstance(val, cls):
            return val
        s = str(val).strip().lower()
        for p in cls:
            if p.value == s:
                return p
        return cls.AGENT


@dataclass
class ModelProfile:
    """单个模型的连接信息（chat / embed / rerank 通用）。

    provider 用于告诉 backend「用哪种方式连」：
      - chat：固定走 OpenAI 兼容协议（"" 等同 openai-compatible）
      - embed：``openai`` / ``ollama`` / ``local``
      - rerank：``local``(CrossEncoder) / ``openai``(/v1/rerank)
    """

    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0

    # OpenAI 兼容协议的通用透传
    headers: Optional[dict] = None
    default_params: Optional[dict] = None
    extra_body: Optional[dict] = None
    reasoning_content_policy: str = "auto"

    # 本地模型目录（embed/rerank 的 local provider 用）
    local_dir: str = ""

    # backend 选择（chat 默认空=OpenAI 兼容；embed/rerank 见上）
    provider: str = ""


@dataclass
class RateLimitConfig:
    """前置限流配置（调用「之前」节流，而非撞 429 再退避）。

    - rpm：每分钟最大请求数（0 = 不限）
    - tpm：每分钟最大 token 数（0 = 不限），实际用 ``len(text)//4`` 估算输入 token

    注意：限流是「预防性」的，目的是把突发流量削平，避免上游限流。
    """

    rpm: int = 0
    tpm: int = 0


@dataclass
class ChatModelSpec:
    """一个 chat 用途的完整定义。

    - primary：首选模型
    - fallbacks：primary 持续失败（熔断开闸）时按顺序尝试的备用模型
    - limits：该用途的限流
    """

    purpose: Purpose
    primary: ModelProfile
    fallbacks: list = field(default_factory=list)
    limits: RateLimitConfig = field(default_factory=RateLimitConfig)

    def ordered_profiles(self) -> list:
        """降级顺序：主模型永远在最前。"""
        return [self.primary, *self.fallbacks]


@dataclass
class EmbedSpec:
    """embedding 用途定义（记忆系统用）。

    provider 取值：
      - ``openai`` / ``remote``：OpenAI 兼容 ``/v1/embeddings``
      - ``ollama``：Ollama 原生 ``/api/embeddings``（用 ``prompt`` 字段）
      - ``local``：本地 ``sentence_transformers.SentenceTransformer``
    """

    purpose: Purpose = Purpose.MEMORY
    provider: str = "local"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    local_model: str = ""   # sentence_transformers 模型名（provider=local 时）
    limits: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class RerankSpec:
    """rerank 用途定义（记忆系统精排用）。

    provider 取值：
      - ``local``：本地 ``sentence_transformers.CrossEncoder``（零成本，推荐）
      - ``openai``：OpenAI 兼容 ``/v1/rerank``
    """

    purpose: Purpose = Purpose.MEMORY
    provider: str = "local"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    local_dir: str = ""      # 本地 CrossEncoder 目录（零网络加载）
    limits: RateLimitConfig = field(default_factory=RateLimitConfig)
