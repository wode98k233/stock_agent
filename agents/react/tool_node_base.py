"""ReAct 工具节点基础执行层。

职责：并行执行 LLM 返回的 tool_calls，处理同迭代去重、历史结果复用、
工具输出精简/压缩，以及压缩结果回填 ExecutionState。

不处理：主 ReAct 的市场能力过滤、子 ReAct 的 step 目标提示。这些差异由子类钩子实现。
"""

import asyncio
from typing import Optional

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig

from agents.react.tool_reuse import find_reusable_tool_call, tool_signature
from agents.react.utils import (
    PerToolRateLimiter,
    _compress_tool_output,
    _hard_truncate_keep_both_ends,
    _trim_tool_output,
)
from agents.react import tool_compressor
from config import Config
from memory.metadata import extract_tool_metadata, push_tool_extract
from utils.cancellation import raise_if_cancelled
from utils.llm_factory import _set_recorder_label
from utils.logger import ensure_radar


class BaseToolNode:
    """主 ReAct / 子 ReAct 共享的工具执行骨架。"""

    def __init__(self, exec_state, tools=None, logger=None, rate_limiter: Optional[PerToolRateLimiter] = None):
        self.exec_state = exec_state
        self.tools = tools if tools is not None else []
        self._logger = ensure_radar(logger)
        self._rate_limiter = rate_limiter or PerToolRateLimiter()

    def get_config(self):
        """返回配置对象。子类可重写，方便测试按原模块 patch Config。"""
        return Config

    def trim_tool_output(self, tool_name: str, content: str) -> str:
        return _trim_tool_output(tool_name, content)

    async def compress_tool_output(self, tool_name: str, content: str, user_goal: str = "") -> str:
        """压缩前加一道「首批预热」门闩：同一次 run 内只让第一条压缩调用先单独跑。

        背景（2026-09-28 实测）：一轮内多条超长输出的压缩调用并发发出时全部冷启动
        （provider prompt cache 全 miss）；让第一条先完成（写入缓存）再放行其余，
        可回收首批的冷启动损失。门闩挂在 exec_state 上（每次 run 全新实例），
        run 内只生效一次；event 置位后走快路径，零额外开销。
        """
        event = getattr(self.exec_state, "_compress_warm_event", None)
        if event is None:
            event = asyncio.Event()
            self.exec_state._compress_warm_event = event
        if event.is_set():
            # 已暖：直接并行
            return await _compress_tool_output(tool_name, content, self._logger, user_goal=user_goal)
        if not getattr(self.exec_state, "_compress_warm_claimed", False):
            # 同 tick 内「检查 + 置位」无竞态（asyncio 单线程）；首条独占执行
            self.exec_state._compress_warm_claimed = True
            try:
                return await _compress_tool_output(tool_name, content, self._logger, user_goal=user_goal)
            finally:
                event.set()
        await event.wait()
        return await _compress_tool_output(tool_name, content, self._logger, user_goal=user_goal)

    async def _call_tool(self, tool, args: dict, config: RunnableConfig | None):
        if hasattr(tool, "ainvoke"):
            return await tool.ainvoke(args, config=config)
        return await asyncio.to_thread(tool.invoke, args)

    def _tool_call_id(self, tool_call: dict, idx: int) -> str:
        return tool_call.get("id") or tool_call.get("tool_call_id") or f"tool_call_{idx}"

    def _preflight_tool_call(
        self,
        tool_name: str,
        tool_args: dict,
        call_id: str,
        state: dict,
        exec_state,
    ) -> tuple[ToolMessage, bool] | None:
        """工具执行前的子类钩子。返回结果表示跳过真实工具执行。"""
        return None

    async def _run_one(
        self,
        tool_call: dict,
        idx: int,
        config: RunnableConfig | None,
        tools_by_name: dict,
        state: dict,
    ) -> tuple[ToolMessage, bool]:
        tool_name = tool_call.get("name", "unknown")
        tool_args = tool_call.get("args") or {}
        call_id = self._tool_call_id(tool_call, idx)
        exec_state = self.exec_state

        preflight = self._preflight_tool_call(tool_name, tool_args, call_id, state, exec_state)
        if preflight is not None:
            return preflight

        runtime_config = self.get_config()
        if runtime_config.REACT_DEDUP_MODE == "exact":
            reused = find_reusable_tool_call(exec_state, tool_name, tool_args)
            if reused is not None:
                content = str(reused.tool_output or "")
                self._logger.info("R", f"复用历史工具结果: {tool_signature(tool_name, tool_args)}")
                return ToolMessage(content=content, name=tool_name, tool_call_id=call_id), True

        raw_output = None
        compress_stages = []
        compress_metrics = None
        tool = tools_by_name.get(tool_name)
        if tool is None:
            content = f"执行失败: 未找到工具 {tool_name}"
        else:
            try:
                await self._rate_limiter.wait(tool_name)
                output = await self._call_tool(tool, tool_args, config)
                content = str(output)

                # ── 记忆元数据提取: 从工具返回值中提取结构化字段 ──
                try:
                    extract = extract_tool_metadata(tool_name, tool_args, content)
                    if extract:
                        push_tool_extract(extract)
                except Exception:
                    pass

                # ── 图表萃取（Track 1 规则）：trim 前拦截完整 tables，非阻塞毫秒级。
                #    失败仅静默跳过，绝不影响 agent 流程与工具输出。
                #    提示不立即追加——压缩会替换 content 丢掉标记，改为流程末尾统一追加。
                chart_hint = ""
                try:
                    if tool_name == "mx_data_query":
                        from agents.analysis.chart_extractor import extract_from_tool_output
                        chart_ids = extract_from_tool_output(tool_name, content)
                        if chart_ids:
                            # 紧凑提示（约 60 token）：带 @@CHART:uuid@@ 标记，
                            # 供 LLM 引用与 report_builder 追加到报告末尾
                            markers = " ".join(f"@@CHART:{cid}@@" for cid in chart_ids)
                            chart_hint = f"\n📊 已自动生成图表: {len(chart_ids)} 张 {markers}"
                except Exception:
                    pass

                # ── 规则压缩（Step 2）：off/shadow/on 三态，先于 trim。
                #    shadow=只计算不采用（对比落盘），on=采用规则输出；异常静默回退原文。
                rule_raw_len = len(content)
                rule_raw_content = content
                rule_output = None
                rule_mode = tool_compressor.get_mode(tool_name)
                rule_len = None
                rule_lossy = False
                if rule_mode != tool_compressor.MODE_OFF:
                    rule_candidate = tool_compressor.compress(tool_name, tool_args, content)
                    if (
                        isinstance(rule_candidate, str)
                        and rule_candidate != content
                        and len(rule_candidate) < len(content)
                    ):
                        rule_len = len(rule_candidate)
                        rule_output = rule_candidate
                        rule_lossy = tool_compressor.is_lossy_output(tool_name, rule_candidate)
                        if rule_mode == tool_compressor.MODE_ON and not rule_lossy:
                            raw_output = content
                            content = rule_candidate
                            compress_stages.append("rule")
                            self._logger.debug(
                                "R", f"规则压缩: {tool_name} {rule_raw_len}→{rule_len}字符"
                            )
                        elif rule_mode == tool_compressor.MODE_ON:
                            self._logger.info(
                                "R", f"规则压缩有损，回落 LLM 流程: {tool_name} {rule_raw_len}→{rule_len}字符"
                            )

                original_len = len(content)
                content = self.trim_tool_output(tool_name, content)
                if len(content) < original_len:
                    raw_output = content
                    compress_stages.append("trim")
                    self._logger.debug("R", f"工具输出精简: {tool_name} {original_len}→{len(content)}字符")

                hard_limit = runtime_config.TOOL_HARD_TRUNCATE_CHARS
                if hard_limit > 0 and len(content) > hard_limit:
                    raw_output = raw_output or content
                    # 保留两头（头部字段名 + 尾部最近数据）并标注省略量。
                    # 原为 content[:hard_limit] 纯头截断，会把按时间升序的序列
                    # （如 get_etf_history 的 K 线）最近一段砍掉 → 报告误报「未覆盖当日」。
                    content = _hard_truncate_keep_both_ends(content, hard_limit)
                    compress_stages.append("hard")

                threshold = runtime_config.get_tool_compress_threshold(tool_name)
                self._logger.debug("R", f"工具输出阈值检查: {tool_name} len={len(content)} threshold={threshold}")
                llm_used = False
                if threshold > 0 and len(content) > threshold:
                    llm_used = True
                    raw_output = raw_output or content
                    compress_stages.append("llm")
                    # 传入用户目标：压缩 prompt 据此优先保留与目标相关的数据
                    # （原实现从未传该参数，goal_hint 一直是空的）
                    try:
                        user_goal = str(state.get("input") or "")
                    except Exception:
                        user_goal = ""
                    # 压缩是一次独立的 LLM 调用；它不经过 agent 节点的调用包装，
                    # 若不刷新 label，会沿用 agent 的标签（日志里被算进 agent 成本）。
                    # 重试由 _compress_tool_output 内部的 ainvoke_with_retry 负责。
                    _set_recorder_label(config, "react-tool-compress")
                    content = await self.compress_tool_output(tool_name, content, user_goal=user_goal)

                # 压缩/截断后统一追加图表提示，确保标记始终在最终工具输出中
                if chart_hint:
                    content = content + chart_hint

                # ── 压缩埋点：记录压缩前/后长度与方式，供事后按工具分析。
                #    raw_len 也可从 steps.output（原文）反推，此处一并落库便于单条 SQL 定位。
                compress_metrics = {
                    "raw_len": rule_raw_len,
                    "final_len": len(content),
                    "compress_method": "+".join(compress_stages) or "none",
                }

                if rule_mode != tool_compressor.MODE_OFF and rule_len is not None:
                    tool_compressor.log_shadow_event(
                        tool_name, tool_args, rule_mode, rule_raw_len, rule_len,
                        len(content), llm_used, threshold,
                        raw_content=rule_raw_content,
                        rule_content=rule_output or "",
                        final_content=content,
                        rule_lossy=rule_lossy,
                    )
            except Exception as e:
                content = f"执行失败: {e}"

        # ── 压缩埋点回填：tool step 的 event_run_id == exec_state 该 ToolCall 的 tool_call_id ──
        if compress_metrics is not None:
            try:
                from utils.agent_trace.core import get_active_recorder
                rec = get_active_recorder()
                if rec is not None:
                    tool_input_str = str(tool_args)
                    run_id = None
                    for call in reversed(exec_state.tool_calls):
                        if call.tool_name == tool_name and call.tool_input == tool_input_str:
                            run_id = call.tool_call_id
                            break
                    if run_id:
                        rec.update_tool_step_metrics(run_id, compress_metrics)
            except Exception:
                pass

        if raw_output is not None:
            if not exec_state.update_tool_call_by_id(call_id, content, raw_output=raw_output):
                tool_input_str = str(tool_args)
                for call in reversed(exec_state.tool_calls):
                    if call.tool_name == tool_name and call.tool_input == tool_input_str:
                        exec_state.update_tool_call(call, content, raw_output=raw_output)
                        break

        return ToolMessage(content=content, name=tool_name, tool_call_id=call_id), False

    async def __call__(self, state: dict, config: RunnableConfig | None = None) -> dict:
        """执行最后一条 AIMessage 中的 tool_calls。"""
        raise_if_cancelled()
        messages = state["messages"]
        last_msg = messages[-1]
        tools_by_name = {tool.name: tool for tool in self.tools}

        tool_calls = getattr(last_msg, "tool_calls", None) or []
        original_count = len(tool_calls)

        sig_to_first = {}
        idx_to_repr = {}
        deduped_tasks = []
        for idx, tc in enumerate(tool_calls):
            sig = tool_signature(tc.get("name", ""), tc.get("args", {}))
            if sig in sig_to_first:
                idx_to_repr[idx] = sig_to_first[sig]
            else:
                sig_to_first[sig] = idx
                idx_to_repr[idx] = idx
                deduped_tasks.append((idx, tc))

        dedup_count = original_count - len(deduped_tasks)
        if self._logger and tool_calls:
            names = [tc.get("name", "?") for tc in tool_calls]
            if dedup_count > 0:
                self._logger.info("R", f"并行执行 {len(deduped_tasks)}/{original_count} 个工具（去重 {dedup_count}）: {names}")
            else:
                self._logger.info("R", f"并行执行 {original_count} 个工具: {names}")

        results = await asyncio.gather(*[
            self._run_one(tc, idx, config, tools_by_name, state)
            for idx, tc in deduped_tasks
        ])

        tool_messages_from_run = [r[0] for r in results]
        cache_hit_flags = [r[1] for r in results]

        cache_hit_names = []
        for (idx, tc), is_hit in zip(deduped_tasks, cache_hit_flags):
            if is_hit:
                cache_hit_names.append(tc.get("name", "unknown"))

        repr_results = {}
        for (idx, _), tm in zip(deduped_tasks, tool_messages_from_run):
            repr_results[idx] = tm

        tool_messages = []
        for idx in range(len(tool_calls)):
            repr_idx = idx_to_repr[idx]
            result = repr_results.get(repr_idx)
            if result is None:
                tc = tool_calls[idx]
                result = ToolMessage(
                    content="执行异常",
                    name=tc.get("name", ""),
                    tool_call_id=self._tool_call_id(tc, idx),
                )
            elif idx != repr_idx:
                result = result.model_copy(
                    update={"tool_call_id": self._tool_call_id(tool_calls[idx], idx)}
                )
            tool_messages.append(result)

        return {
            "messages": list(tool_messages),
            "tool_calls_count": state["tool_calls_count"] + len(tool_messages),
            "cache_hit_tools": cache_hit_names,
        }
