"""从 agent_trace 库取**全量**证据。

与 `eval/review/collector.py` 的关键区别：collector 把工具返回裁到 1200 字喂 judge，
这里**直查 `steps.output` 原文**，因为「核验回答里的数字有没有出处」在裁剪后做不了。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

# 取证池上限：防止把整个 trace 塞进 prompt。远超 digest 的 24000，够核验用。
MAX_EVIDENCE_CHARS = 200_000

_SKILL_STEP = "skill"
_LLM_STEP = "llm"
_TOOL_STEP = "tool"


def _loads(value):
    """trace 里的 input/output/extra 都是 JSON 字符串，尽量解成对象。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


@dataclass(frozen=True)
class ToolEvidence:
    """一次工具调用的完整证据。"""

    step_ref: int
    tool: str
    args: str
    status: str
    error: str
    output: str          # 全量，不截断

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def output_chars(self) -> int:
        return len(self.output)

    def to_dict(self) -> dict:
        return {"step_ref": self.step_ref, "tool": self.tool, "args": self.args,
                "status": self.status, "error": self.error,
                "output_chars": self.output_chars}


@dataclass(frozen=True)
class RunEvidence:
    """一次 run 的全量证据集合。"""

    run_id: str
    agent_name: str
    question: str
    final_answer: str
    created_at: str
    status: str
    duration_ms: float | None
    error: str
    tools: tuple[ToolEvidence, ...]
    think_reasoning: tuple[str, ...]
    skill_events: tuple[dict, ...]
    # 运行指标（llm 步统计，口径与 AgentRunner._collect_from_trace 一致）
    iteration_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tool_outputs_text(self) -> str:
        """全部工具返回拼接（取证池：数字出处的查找范围）。"""
        return "\n".join(t.output for t in self.tools if t.output)

    @property
    def whitelist_text(self) -> str:
        """来源白名单 = 工具输出 ∪ 用户问题。

        必须含用户问题：用户自己在提问里给的数字（如「我成本 1680」）不是幻觉，
        不该被判 unverified。
        """
        parts = [self.tool_outputs_text, self.question]
        return "\n".join(p for p in parts if p)

    @property
    def tools_by_ref(self) -> dict[int, ToolEvidence]:
        return {t.step_ref: t for t in self.tools}

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "agent_name": self.agent_name,
            "question": self.question, "final_answer": self.final_answer,
            "created_at": self.created_at, "status": self.status,
            "duration_ms": self.duration_ms, "error": self.error,
            "tools": [t.to_dict() for t in self.tools],
            "think_reasoning_count": len(self.think_reasoning),
            "skill_events": list(self.skill_events),
        }


def load_run_evidence(conn: sqlite3.Connection, run_id: str) -> RunEvidence:
    """读取一个 run 的全量证据；run 不存在时抛 ValueError。

    契约（分库后）：conn 必须是**该 run 所在月库**的连接
    （生产侧经 `utils.agent_trace.store.store.conn_for_run(run_id)` 获取）。
    """
    run = conn.execute(
        "SELECT id, agent_name, input, output, status, error, created_at, duration_ms"
        " FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"trace run 不存在: {run_id}")

    steps = conn.execute(
        "SELECT id, step_type, step_name, status, error, input, output, extra"
        " FROM steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    tools: list[ToolEvidence] = []
    think: list[str] = []
    skills: list[dict] = []
    tokens_in = tokens_out = 0

    for step in steps:
        stype = step["step_type"] or ""
        if stype == _TOOL_STEP:
            tools.append(ToolEvidence(
                step_ref=step["id"],
                tool=step["step_name"] or "unknown",
                args=_as_text(_loads(step["input"])),
                status=step["status"] or "unknown",
                error=_as_text(step["error"]),
                output=_as_text(_loads(step["output"])),
            ))
        elif stype == _LLM_STEP:
            tokens_in += _llm_tokens(step, "input_tokens")
            tokens_out += _llm_tokens(step, "output_tokens")
            row = conn.execute(
                "SELECT reasoning FROM messages WHERE step_id=? AND role='ai'"
                " ORDER BY seq DESC LIMIT 1", (step["id"],)).fetchone()
            reasoning = (row["reasoning"] if row else "") or ""
            if reasoning:
                think.append(reasoning)
        elif stype == _SKILL_STEP:
            extra = _loads(step["extra"]) or {}
            if isinstance(extra, dict):
                skills.append({
                    "step_ref": step["id"],
                    "event": step["step_name"] or extra.get("kind") or "select",
                    "selected": extra.get("selected") or [],
                    "reason": extra.get("reason") or "",
                    "market": extra.get("market") or "",
                    "active_skill": extra.get("active_skill") or "",
                })

    return RunEvidence(
        run_id=run["id"],
        agent_name=run["agent_name"] or "",
        question=_as_text(_loads(run["input"])),
        final_answer=_as_text(_loads(run["output"])),
        created_at=run["created_at"] or "",
        status=run["status"] or "",
        duration_ms=run["duration_ms"],
        error=_as_text(run["error"]),
        tools=tuple(tools),
        think_reasoning=tuple(think),
        skill_events=tuple(skills),
        iteration_count=len([s for s in steps if (s["step_type"] or "") == _LLM_STEP]),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _llm_tokens(step: sqlite3.Row, key: str) -> int:
    """从 llm 步的 extra JSON 里取 token 用量；缺失/损坏记 0。"""
    extra = _loads(step["extra"])
    if not isinstance(extra, dict):
        return 0
    try:
        return int((extra.get("token_usage") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def text_at_step(conn: sqlite3.Connection, run_id: str, step_ref: int) -> str:
    """取某一步的原文，供 quote 校验用（工具步取 output，LLM 步取 reasoning/content）。"""
    step = conn.execute(
        "SELECT id, step_type, output FROM steps WHERE run_id=? AND id=?",
        (run_id, step_ref)).fetchone()
    if step is None:
        return ""
    if (step["step_type"] or "") == _LLM_STEP:
        rows = conn.execute(
            "SELECT reasoning, content FROM messages WHERE step_id=? AND role='ai'"
            " ORDER BY seq DESC", (step_ref,)).fetchall()
        parts = []
        for r in rows:
            if r["reasoning"]:
                parts.append(r["reasoning"])
            if r["content"]:
                parts.append(_as_text(_loads(r["content"])))
        return "\n".join(parts)
    return _as_text(_loads(step["output"]))


def run_created_at(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT created_at FROM runs WHERE id=?", (run_id,)).fetchone()
    return (row["created_at"] if row else "") or ""


def list_run_ids(store, *, agent_name: str = "",
                 since: str = "", until: str = "",
                 limit: int | None = None) -> list[str]:
    """按条件列出 run id（存量回测的目标筛选），时间倒序（跨月逐库归并）。

    分库后第一参数为 `utils.agent_trace.store.store`（不再接收单库 conn）。
    """
    runs = store.list_runs(
        limit=limit if limit is not None else 10_000,
        status="success", agent_name=agent_name, since=since, until=until,
    )
    return [r["id"] for r in runs]
