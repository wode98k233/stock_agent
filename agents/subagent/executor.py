"""最小子代理执行器（M2）：空上下文 + 指定工具面 + 结构化结论卡。

刻意不接 LangGraph（现有 ReAct 图入口硬编码 classify→prepare，会拉父历史），
而是自建一个短循环，对应 DSH `subagent-spawn-in-process` 的「新子 Agent」形态。

依赖注入：`llm` 只需实现 `.invoke(messages)`（可选 `.bind_tools`）；
`tools` 是 name → 可调用对象（LangChain tool 或普通函数）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

from agents.subagent.models import (
    STATUS_ABORTED, STATUS_ERROR, STATUS_MAX_TURNS, STATUS_NO_DATA, STATUS_OK,
    STATUS_TIMEOUT, STOP_ABORTED, STOP_COMPLETED, STOP_ERROR, STOP_MAX_TURNS,
    STOP_NO_DATA, STOP_TIMEOUT, SubagentResult, SubagentTask, clip_diagnostic,
)

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是证券研究的数据取数员。你只能完成被指派的一个数据任务，看不到其它对话。\n"
    "规则：\n"
    "- 必须调用工具取真实数据，禁止凭记忆或常识作答；\n"
    "- 取不到就如实说「取不到」，不要编造数字，不要用 0 或「约」代替；\n"
    "- 拿到数据后，按给定 JSON Schema 输出结论卡；数字必须带来源与日期；\n"
    "- 只输出 JSON，不要解释文字。"
)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

try:  # 用真实消息对象，tuple 形式带 tool_call_id 会被 LangChain 拒绝
    from langchain_core.messages import AIMessage as _AIMessage, ToolMessage as _ToolMessage
except Exception:  # pragma: no cover - 环境缺 langchain 时降级为二元组
    _AIMessage = _ToolMessage = None


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    raw = m.group(1) if m else text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        val = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return val if isinstance(val, dict) else None


class SubagentExecutor:
    def __init__(self, *, llm, tools: Mapping[str, Any], log=None,
                 max_default_turns: int = 12, deadline_s: float = 180.0):
        self.llm = llm
        self.tools = dict(tools)
        self.log = log or logger
        self.max_default_turns = max_default_turns
        self.deadline_s = deadline_s

    # ── 工具面 ──
    def _llm_for(self, task: SubagentTask):
        names = task.tool_whitelist or tuple(self.tools)
        subset = [self.tools[n] for n in names if n in self.tools]
        if not subset:
            return self.llm
        bind = getattr(self.llm, "bind_tools", None)
        if bind is None:
            return self.llm
        try:
            return bind(subset)
        except Exception as e:  # 工具绑定失败 → 无工具跑（会在后面判 no_data）
            logger.warning("bind_tools 失败: %s", e)
            return self.llm

    def _call_tool(self, name: str, args: Dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return json.dumps({"error": f"未注册工具: {name}"}, ensure_ascii=False)
        try:
            if hasattr(tool, "invoke"):
                out = tool.invoke(args)
            elif callable(tool):
                out = tool(**args)
            else:
                out = None
            return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001 —— 工具失败是数据缺口，不是崩溃
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

    def _invoke(self, llm, messages, label: str):
        """统一调用入口：有 RadarLogger 时走 tracked_invoke（带 label → trace/成本可分步）。

        没有 RadarLogger（单测/离线）时退回裸 invoke —— 不引入计量依赖。
        """
        try:
            from utils.logger import RadarLogger
            if isinstance(self.log, RadarLogger):
                from utils.llm_factory import tracked_invoke
                # ⚠️ trace step 的 label 来自 config.metadata（TokenRecorder 是另一条路）：
                # 只传 label= 不传 metadata=，steps.extra.label 会回落成模型名。
                return tracked_invoke(llm, messages, self.log, label=label,
                                      metadata={"label": label, "purpose": "deep_report",
                                                "node": "deep_fetch"})
        except Exception as e:  # noqa: BLE001 —— 计量失败不能影响取数
            logger.warning("tracked_invoke 不可用，退回裸调用: %s", e)
        return llm.invoke(messages)

    # ── 主循环 ──
    def run(self, task: SubagentTask, *, cancel: Optional[threading.Event] = None) -> SubagentResult:
        started = time.time()
        max_turns = max(1, int(task.max_turns or self.max_default_turns))
        limit_s = float(task.deadline_s if task.deadline_s is not None else self.deadline_s)
        llm = self._llm_for(task)
        messages: List[Any] = [
            ("system", _SYSTEM),
            ("user", f"任务：{task.goal}\n\n输出 Schema：\n{json.dumps(task.output_schema, ensure_ascii=False)}"),
        ]
        tool_calls = 0
        turns = 0
        last_text = ""
        tokens: Dict[str, int] = {}
        finished = False   # 模型主动收尾（某轮没有任何 tool_call）

        try:
            for turn in range(1, max_turns + 1):
                if cancel is not None and cancel.is_set():
                    return SubagentResult(STATUS_ABORTED, STOP_ABORTED, last_text,
                                          tool_calls=tool_calls, turns=turns, tokens=tokens)
                if time.time() - started >= limit_s:
                    return SubagentResult(STATUS_TIMEOUT, STOP_TIMEOUT, last_text,
                                          tool_calls=tool_calls, turns=turns, tokens=tokens)

                resp = self._invoke(llm, messages, f"deep-fetch:{task.slot}:t{turn}")
                turns = turn
                content = getattr(resp, "content", None) or ""
                if isinstance(content, list):  # 部分 provider 返回 content blocks
                    content = "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b)
                                      for b in content)
                last_text = str(content or "")
                self._merge_tokens(tokens, resp)

                calls = getattr(resp, "tool_calls", None) or []
                if calls and _AIMessage is not None:
                    messages.append(_AIMessage(content=last_text, tool_calls=calls))
                else:
                    messages.append(("assistant", last_text))
                if not calls:
                    finished = True
                    break

                for call in calls:
                    name, args, cid = self._parse_call(call)
                    result = self._call_tool(name, args)
                    tool_calls += 1
                    if _ToolMessage is not None:
                        messages.append(_ToolMessage(content=result[:4000],
                                                     tool_call_id=cid or name, name=name))
                    else:
                        messages.append(("tool", f"[{name}] {result[:4000]}"))

            # ── 结算 ──
            # ① 没跑完就被回合上限截断 → max_turns（不是 error）
            if not finished and tool_calls > 0:
                return SubagentResult(STATUS_MAX_TURNS, STOP_MAX_TURNS, last_text,
                                      tool_calls=tool_calls, turns=turns, tokens=tokens,
                                      diagnostic=clip_diagnostic("达到 max_turns 上限"))
            # ② 一次工具都没调 → 无论模型说什么都判 no_data（防伪成功、防编数）
            if tool_calls == 0:
                return SubagentResult(STATUS_NO_DATA, STOP_NO_DATA, last_text,
                                      tool_calls=0, turns=turns, tokens=tokens,
                                      diagnostic=clip_diagnostic("回合内未调用任何工具"))

            structured = _parse_json(last_text)
            if structured is None:
                structured = self._ask_structured(llm, messages, task)
            if structured is None:
                return SubagentResult(
                    STATUS_NO_DATA if tool_calls == 0 else STATUS_ERROR,
                    STOP_NO_DATA if tool_calls == 0 else STOP_ERROR,
                    last_text, tool_calls=tool_calls, turns=turns, tokens=tokens,
                    diagnostic=clip_diagnostic("未拿到符合 schema 的结论卡"),
                )
            return SubagentResult(STATUS_OK, STOP_COMPLETED, last_text, structured=structured,
                                  tool_calls=tool_calls, turns=turns, tokens=tokens)
        except Exception as e:  # noqa: BLE001 —— 绝不穿透
            logger.warning("子代理失败 slot=%s: %s", task.slot, e)
            return SubagentResult(STATUS_ERROR, STOP_ERROR, last_text,
                                  tool_calls=tool_calls, turns=turns, tokens=tokens,
                                  diagnostic=clip_diagnostic(f"{type(e).__name__}: {e}"))

    # ── 辅助 ──
    def _ask_structured(self, llm, messages: List[Any], task: SubagentTask) -> Optional[Dict[str, Any]]:
        try:
            again = messages + [("user", "请只输出符合 Schema 的 JSON 结论卡，不要任何解释。")]
            resp = self._invoke(llm, again, f"deep-fetch:{task.slot}:structured")
            text = getattr(resp, "content", None) or ""
            return _parse_json(str(text))
        except Exception:
            return None

    @staticmethod
    def _parse_call(call: Any):
        if isinstance(call, dict):
            return call.get("name", ""), call.get("args") or {}, call.get("id")
        return (getattr(call, "name", ""), getattr(call, "args", {}) or {},
                getattr(call, "id", None))

    @staticmethod
    def _merge_tokens(acc: Dict[str, int], resp: Any) -> None:
        usage = getattr(resp, "usage_metadata", None) or {}
        if not isinstance(usage, dict):
            return
        for src, dst in (("input_tokens", "input"), ("output_tokens", "output"),
                         ("cache_read_input_tokens", "cache_read"),
                         ("cache_creation_input_tokens", "cache_write")):
            val = usage.get(src)
            if isinstance(val, (int, float)):
                acc[dst] = acc.get(dst, 0) + int(val)
