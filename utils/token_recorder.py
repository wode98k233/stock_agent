"""统一 Token 记录器 — 三位一体架构

设计目标：
- metrics、budget、trace 从同一事件获取相同 token 数
- 可插拔后端：新增记录目标只需实现 TokenBackend
- 业务层屏蔽：tracked_invoke 只需传入 TokenRecorder，无需知道后端细节

Trace 通过 register_configure_hook 全局注入，独立于此模块。
"""
from __future__ import annotations

import time
import inspect
from typing import Protocol

from langchain_core.callbacks.base import BaseCallbackHandler

from utils.budget import BudgetExceeded
from utils.logger import ensure_radar
from utils.token_usage import extract_base_url, extract_model_name, extract_token_usage


def _prefix_fingerprint(response) -> str:
    """取本次请求的 prompt 前缀指纹（tools + system 的 SHA-256 前 16 位）。

    由 ``TokenCompatibleChatOpenAI._create_chat_result`` 回填到 ``llm_output``。
    缺失（老路径 / 非本项目模型）时返回空串，调用方据此省略日志字段。
    """
    out = getattr(response, "llm_output", None)
    if not isinstance(out, dict):
        return ""
    fp = out.get("prefix_sha256")
    if not fp:
        return ""
    size = out.get("prefix_bytes")
    return f"{fp}/{size}B" if size else str(fp)


class TokenBackend(Protocol):
    """Token 记录后端接口

    `provider` / `cost` / `ts` 是**加性**参数（默认值齐全）：既有后端不受影响，
    新后端可以据此做成本聚合（设计文档 §7）。

    cache_read / cache_creation 用于 prompt 缓存命中观测（见 PRD-A9 M-1）：
    cache_read 是按缓存折扣价计费的输入 token，cache_creation 是按写入溢价计费的
    输入 token。两者都不代表额外消耗，但能反推前缀是否稳定。
    """
    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0, model: str = "", provider: str = "",
               cost: dict | None = None, ts: float | None = None,
               cache_read: int = 0, cache_creation: int = 0) -> None: ...


class MetricsBackend(TokenBackend):
    """记录到 RequestContext（metrics 汇总）"""
    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0, model: str = "", provider: str = "",
               cost: dict | None = None, ts: float | None = None,
               cache_read: int = 0, cache_creation: int = 0) -> None:
        try:
            from utils.logger import RequestContext
            ctx = RequestContext.current()
            if ctx:
                ctx.record_llm(tokens_in, tokens_out, cached_tokens, reasoning_tokens,
                               label, model=model, provider=provider, cost=cost,
                               cache_read=cache_read, cache_creation=cache_creation)
        except Exception:
            pass


def weight_of(*, tokens_in: int, tokens_out: int, cached: int,
              billing: str = "", cost: dict | None = None) -> float:
    """把一次调用折算成**计费口径** token（PRD-A11 FR-6）。

    规则（从价目快照读，不手填）：
      · ``billing == free``        → 免费端点，**整体不计费**（权重 0）
      · 付费端点的**输入命中部分** → 按 ``in_hit / in_miss`` 折算（实测 1/50）
      · 其余（未命中输入 + 输出）  → 1.0

    为什么要折算：命中价是未命中的 1/50、免费端点 ¥0，但它们过去都按原始 token
    100% 占预算 —— 结果是"缓存优化被预算惩罚"、「用命中换上下文长度」会更快撞
    预算上限，且免费调用白占额度（实测占 raw 的 26%）。

    ⚠️ 不引入新的配置开关当"总闸"：权重由 ``cost`` 快照推出；``cost`` 缺失时
    退回 1.0（保守，与改动前一致）。``BUDGET_FREE_WEIGHT`` 只用于把免费端点从
    "0 计"改成"低计"这类灰度需求。
    """
    total = tokens_in + tokens_out
    if billing == "free":
        try:
            from config import Config
            w = float(getattr(Config, "BUDGET_FREE_WEIGHT", 0.0) or 0.0)
        except Exception:
            w = 0.0
        return total * w

    if not cached or not tokens_in:
        return float(total)

    # 权重来源优先级：调用方快照 → 配置 → 价目表 → 兜底。
    # ⚠️ 必须用 `is None` 而不是 `if not`：配置成 **0** 是"显式不折算"的合法取值，
    # 用 falsy 判断会让 0 被兜底覆盖，"关掉折算"永远关不掉（实测踩过）。
    hit_weight = None
    if isinstance(cost, dict):
        got = cost.get("cache_hit_weight")
        if isinstance(got, (int, float)):
            hit_weight = float(got)
    if hit_weight is None:
        try:
            from config import Config
            got = getattr(Config, "BUDGET_CACHE_HIT_WEIGHT", None)
            if got is not None:
                hit_weight = float(got)      # 含 0 = 显式不折算
        except Exception:
            hit_weight = None
    if hit_weight is None:
        # 无配置 → 从价目快照按 in_hit/in_miss 折算（不手填）
        try:
            snap = (cost or {}).get("price_snapshot") or {}
            miss, hit = snap.get("in_miss"), snap.get("in_hit")
            if miss and hit:
                hit_weight = float(hit) / float(miss)
        except Exception:
            hit_weight = None
    if hit_weight is None:
        hit_weight = 0.02      # 兜底：与实测 1/50 同量级
    if hit_weight <= 0:
        # 语义对齐：配置成 0 = **关闭折算**（还原"命中按全价占预算"的旧口径），
        # 等价权重 1.0。不这么做的话 0 会被理解成"命中不计费"，与配置文档矛盾。
        hit_weight = 1.0

    miss_in = max(0, tokens_in - cached)
    return float(miss_in + cached * hit_weight + tokens_out)


class BudgetBackend(TokenBackend):
    """记录到 BudgetController。

    PRD-A11 FR-6 起，预算按**计费口径**判定（命中折算、免费端点不计），
    同时把原始 token 一并喂进去做硬上限兜底 —— 两个口径并存，谁都看得见。
    """
    def __init__(self, budget):
        self.budget = budget

    def record(self, tokens_in: int, tokens_out: int, label: str, cached_tokens: int = 0,
               reasoning_tokens: int = 0, model: str = "", provider: str = "",
               cost: dict | None = None, ts: float | None = None,
               cache_read: int = 0, cache_creation: int = 0) -> None:
        # 思考 token 计入总量（思考也是真实消耗，预算不豁免）
        if not self.budget:
            return
        hit = cached_tokens or cache_read or 0
        billing = str((cost or {}).get("billing") or "")
        weighted = weight_of(tokens_in=tokens_in, tokens_out=tokens_out,
                             cached=hit, billing=billing, cost=cost)
        # budget 是公开扩展点：第三方实现可能还是旧签名 add_tokens(count)。
        # 按签名判定，旧实现退化为原行为（与 _accepts_record_kwargs 同一套思路）。
        if _accepts_kwarg(self.budget.add_tokens, "weighted"):
            self.budget.add_tokens(tokens_in + tokens_out, weighted=weighted)
        else:
            self.budget.add_tokens(tokens_in + tokens_out)
        self.budget.add_call()
        self.budget.check(ensure_radar(None))


# 加性参数分组：TokenBackend 是公开扩展点，老后端可能只实现基础签名，
# 传参前按签名逐组判定（两组互相独立，缺哪组就不传哪组）
_COST_KWARGS = ("provider", "cost", "ts")
_CACHE_KWARGS = ("cache_read", "cache_creation")


def _accepts_kwarg(func, name: str) -> bool:
    """``func`` 是否接受名为 ``name`` 的关键字参数（不可 introspect 时保守返回 True）。"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_record_kwargs(backend, names: tuple) -> bool:
    """后端的 ``record`` 是否接受给定的加性关键字参数。

    成本参数（provider / cost / ts，设计文档 §7）与 prompt 缓存观测参数
    （cache_read / cache_creation，PRD-A9 M-1）都是加性扩展，但第三方/既有后端可能
    仍按旧签名实现（``record(in, out, label, cached, reasoning, model)``）。直接传新参数
    会 TypeError，而 ``on_llm_end`` 用 ``except Exception: pass`` 吞掉异常 → 后端静默收不到
    任何记录。因此按签名判定是否传参，旧后端退化为原行为。
    """
    try:
        params = inspect.signature(backend.record).parameters
    except (TypeError, ValueError):
        return True  # 签名不可 introspect（C 扩展等），保守传参
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


class TokenRecorder(BaseCallbackHandler):
    """统一 Token 记录器，可插拔后端

    用法：
        backends = [MetricsBackend()]
        if budget:
            backends.append(BudgetBackend(budget))
        recorder = TokenRecorder(logger, "label", backends)
    """

    def __init__(self, logger, label: str = "", backends: list[TokenBackend] = None, budget=None):
        self.logger = ensure_radar(logger)
        self.label = label
        # 向后兼容：如果传了 budget 但没传 backends，自动构建
        if backends is not None:
            self.backends = backends
        else:
            self.backends = [MetricsBackend()]
            if budget:
                self.backends.append(BudgetBackend(budget))
        self.t0 = None
        self.budget_exceeded = False

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.t0 = time.time()          # 调用**开始时刻**：成本按它判峰谷（§5.4）

    def on_llm_end(self, response, **kwargs):
        dt = time.time() - (self.t0 or time.time())
        usage = extract_token_usage(response)
        inp = usage["input_tokens"]
        out = usage["output_tokens"]

        cached = usage.get("cached_tokens", 0)
        reasoning = usage.get("reasoning_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        # 按上游模型区分统计：从响应本身提取（覆盖网关降级/多模型场景）
        model = extract_model_name(response)

        # 成本：纯函数、无副作用；与 TraceRecorder 各算一次（同一个 ts、同一份价目，
        # 结果确定 —— 共享 state 反而引入耦合，见设计文档 §7）
        # base_url 取本次调用**实际端点**（_create_chat_result 回填到响应上）：
        # 压缩/报告等旁路模型各走各的供应商，不能用全局默认端点反查 —— 否则
        # 旁路调用会被误归到主端点供应商名下（2026-10-15 实测：compress 走
        # sensenova 却全部记到 opencode-zen）。
        cost = None
        try:
            from utils import pricing
            base_url = extract_base_url(response) or pricing.default_base_url()
            breakdown = pricing.compute_cost(usage, model=model,
                                             base_url=base_url, ts=self.t0)
            cost = breakdown.as_dict() if breakdown is not None else None
        except Exception:
            cost = None

        provider = (cost or {}).get("provider", "")
        for backend in self.backends:
            # 按签名逐组判定：老后端只拿到它认识的基础参数，绝不因新参数而静默丢记录
            kwargs = {"model": model}
            if _accepts_record_kwargs(backend, _COST_KWARGS):
                kwargs.update(provider=provider, cost=cost, ts=self.t0)
            if _accepts_record_kwargs(backend, _CACHE_KWARGS):
                kwargs.update(cache_read=cache_read, cache_creation=cache_creation)
            try:
                backend.record(inp, out, self.label, cached, reasoning, **kwargs)
            except BudgetExceeded:
                self.budget_exceeded = True
            except Exception:
                pass

        self.logger.llm_call(self.label, dt, inp, out,
                             cached_tokens=usage.get("cached_tokens", 0),
                             cache_hit_ratio=usage.get("cache_hit_ratio", 0.0),
                             reasoning_tokens=reasoning,
                             cost=cost,
                             cache_read=cache_read,
                             cache_creation=cache_creation,
                             **({"prefix_fp": _prefix_fingerprint(response)} if _prefix_fingerprint(response) else {}),
                             **({"model": model} if model else {}))
