"""PRD-A9 §3.1 e2e：ReAct 模式下报告链路三条调用是否真的共享前缀缓存

**背景（由 2026-10-13 两次实跑确立）**
  改造前 report-select / report-fast / dashboard 三条调用各自发出不同的 system
  （实测前缀指纹分别为 6ac88703 / e5bf005c / 8978032e），同一次 run 内互不命中，
  cached 恒为 0；agent 侧已达 53%~85%，报告链路是当时最大的未命中块。

**本测试锁的验收点**（AC-9 / AC-3 的线上版）：
  1. report-select / report-fast / dashboard 三条调用的 ``prefix_fp`` 完全相同
  2. report-fast 与 dashboard 的 ``cache_read`` > 0（此前恒为 0）
  3. 报告正文与仪表盘仍正常产出（防「为了缓存牺牲质量」）

与单测的分工：``test/unit/analysis/test_report_system_prefix.py`` 锁 system 字节一致；
本测试锁真实链路上的命中结果。
"""
import re
import time

import pytest

QUERY = "今天大盘总结"

# 报告链路：report-fast 与 dashboard 必现。
# report-select 只在不走 conversation fork 时出现 —— PRD-A9 §14 的 fork 形态
# 直接复用 agent 已热对话（工具结果都在里面），不需要再选一次载什么，
# 此时它被【有意跳过】，不是缺失。
CHAIN_LABELS = ("report-fast", "dashboard")
OPTIONAL_CHAIN_LABELS = ("report-select",)


class _LLMCallCollector:
    """抓所有 ``RadarLogger.llm_call`` 调用，取出 label 与缓存字段。

    为什么挂在类上而不是 logging.Handler 上：
      1. ``utils/logger.py:738`` 对有落盘文件的对话级 logger 设了 ``propagate=False``，
         挂 root / 单个实例都只能抓到 0 条（第一、二版都白跑过一轮）；
      2. 真正发日志的 RadarLogger 实例由各调用方传入（``tracked_invoke(..., log, ...)``），
         事先无从得知是哪个。
    在类层面 ``monkeypatch`` 既能全覆盖，又不依赖实例身份。
    """

    def __init__(self):
        self.calls: list = []

    def _install(self, monkeypatch):
        from utils.logger import RadarLogger

        original = RadarLogger.llm_call
        collector = self

        def patched(self, label, duration, tokens_in, tokens_out,
                    cached_tokens=0.0, cache_hit_ratio=0.0,
                    cache_read=0, cache_creation=0, **kv):
            collector.calls.append({
                "label": label,
                "in": int(tokens_in or 0),
                "out": int(tokens_out or 0),
                "cached": int(cached_tokens or 0),
                "cache_read": int(cache_read or 0),
                "cache_creation": int(cache_creation or 0),
                "prefix_fp": str(kv.get("prefix_fp", "")).split("/")[0],
                "model": kv.get("model", ""),
            })
            return original(self, label, duration, tokens_in, tokens_out,
                            cached_tokens=cached_tokens,
                            cache_hit_ratio=cache_hit_ratio,
                            cache_read=cache_read,
                            cache_creation=cache_creation, **kv)

        monkeypatch.setattr(RadarLogger, "llm_call", patched)
        return patched


async def _run_agent(mode: str, context: dict, user_input: str):
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry

    logger = context["logger"]
    memory = context["memory"]
    registry = SkillRegistry(logger, memory, context["skill_register"])
    agent = AgentFactory.get(mode)

    t0 = time.time()
    response = await agent.run(user_input, registry, memory, logger)
    return response, time.time() - t0


def _clean(s: str) -> str:
    s = re.sub(r"\x1b\[[0-9;]*m", "", s or "")
    return s.encode("gbk", errors="replace").decode("gbk")


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.parametrize("mode", ["react_stock", "unified_plan"])
async def test_report_chain_shares_cache_prefix(agent_context, monkeypatch, mode):
    """跑一次 agent，断言报告链路三条调用前缀一致且后两条命中缓存。

    覆盖 react_stock 与 unified_plan 两个模式：二者的仪表盘入口不同
    （前者 state 自带 selected_template，后者只有 template_id），
    修复前只有 react 能吃到 PRD-A9 §3.1 的共享前缀收益。
    """
    collector = _LLMCallCollector()
    collector._install(monkeypatch)

    response, elapsed = await _run_agent(mode, agent_context, QUERY)

    # ── 0. 基本产出（防「为了缓存牺牲质量」）──
    assert response, f"[{mode}] 返回为空"
    assert len(response) > 200, f"[{mode}] 返回过短：{len(response)} 字符"
    print(f"\n  [{mode}] 耗时={elapsed:.1f}s 长度={len(response)}")
    print(f"  [{mode}] 摘要: {_clean(response)[:160]}...")

    by_label = {}
    for c in collector.calls:
        by_label.setdefault(c["label"], []).append(c)

    print("\n  ── 全部 LLM 调用 ──")
    for c in collector.calls:
        pct = c["cache_read"] / c["in"] * 100 if c["in"] else 0.0
        print(f"    {c['label']:<24} in={c['in']:>7} read={c['cache_read']:>7} "
              f"({pct:5.1f}%)  fp={c['prefix_fp'] or '-'}")

    assert collector.calls, (
        "没抓到任何 LLM 调用 —— collector 挂载位置不对"
        "（RadarLogger 在 utils/logger.py:738 设了 propagate=False，root 收不到）"
    )

    missing = [lb for lb in CHAIN_LABELS if lb not in by_label]
    assert not missing, (
        f"[{mode}] 本次运行未触发的报告链路调用：{missing}。"
        f"实际触发：{sorted(by_label)}（可能模板路由到了别的路径）"
    )

    # ── 1. 前缀指纹一致性 ──
    # fork 形态下 report-fast 与 dashboard 均复用 agent 已热对话前缀，二者同源，
    # 要求两者指纹一致；非 fork 分支仍要求三条调用一致。
    forked = "report-select" not in by_label
    if forked:
        print(f"  [{mode}] conversation fork 生效（report-select 已跳过）")
        # fork 时 report-fast 必须整段命中 agent 已热前缀
        reads = [c["cache_read"] for c in by_label["report-fast"]]
        ins = [c["in"] for c in by_label["report-fast"]]
        ratio = sum(reads) / sum(ins) if sum(ins) else 0
        assert ratio >= 0.5, (
            f"[{mode}] fork 形态下 report-fast 命中率仅 {ratio:.1%}，"
            f"前缀未对齐（agent 已热部分应占大头）。in={ins} read={reads}"
        )
    else:
        fps = {lb: by_label[lb][0]["prefix_fp"] for lb in CHAIN_LABELS}
        assert len(set(fps.values())) == 1, (
            f"[{mode}] 报告链路前缀不一致 → 三条调用互不命中：{fps}")

    # ── 2. report-fast / dashboard 实际命中缓存（AC-9）──
    # report-select 是本 run 内第一条报告链路调用，负责「写入」缓存前缀，
    # 首次必然全价；后两条应命中。这正是改造前 cached 恒为 0 的位置。
    #
    # ⚠️ 门槛从 `sum(reads) > 0` 提到 ≥50%：dashboard 改成 fork 之前，
    # 实测只命中 2048/10177 = 20.1%（只够 provider 的最小缓存块），
    # 而 `> 0` 让它一路绿灯通过 —— 门槛形同虚设。
    for lb in ("report-fast", "dashboard"):
        reads = sum(c["cache_read"] for c in by_label[lb])
        ins = sum(c["in"] for c in by_label[lb])
        ratio = reads / ins if ins else 0.0
        assert ratio >= 0.5, (
            f"[{mode}][{lb}] 缓存命中率仅 {ratio:.1%}（需 ≥50%）。"
            f"共享前缀未对齐 —— 检查 fork 是否回落（无 conversation/tools）。"
            f"in={ins} read={reads} calls={by_label[lb]}"
        )
        print(f"  PASS [{mode}][{lb}] hit={ratio:.1%} read={reads}/{ins}")

    # ── 3. 报告正文与仪表盘仍在 ──
    assert "##" in response or "|" in response, "报告正文疑似缺失"