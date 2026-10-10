"""
Agent 运行器
触发 Agent 运行，收集结果 + trace
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import Config
from agents.factory import AgentFactory
from tools.skills import SkillRegistry
from utils.agent_trace.core import _active_recorder
from utils.agent_trace.db import find_latest_trace_run_id
from utils.logger import get_logger, get_child_logger


def parse_case_turns(benchmark_case: Dict) -> List[Dict]:
    """解析题库 case 的轮次脚本（设计文档 §3/§13.1）。

    `turns_json`（JSON 字符串或已解析的 list）为 null / 解析失败 / 空时，
    回退为用 case 顶层字段构造的单轮；长度 1 的脚本轮原样保留（即单轮）。
    每轮统一形状：{turn_index, question, expected_tools, expected_sections}。
    """
    raw = benchmark_case.get("turns_json")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None

    turns: List[Dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            question = (item.get("question") or "").strip()
            if not question:
                continue
            turns.append({
                "turn_index": len(turns) + 1,
                "question": question,
                "expected_tools": item.get("expected_tools") or [],
                "expected_sections": item.get("expected_sections") or [],
            })

    if not turns:
        question = (benchmark_case.get("question") or "").strip()
        if question:
            turns = [{
                "turn_index": 1,
                "question": question,
                "expected_tools": benchmark_case.get("expected_tools") or [],
                "expected_sections": benchmark_case.get("expected_sections") or [],
            }]
    return turns


class AgentRunner:
    """
    触发 Agent 运行，收集结果
    复用 server/agent_runner.py 的模式
    """

    def __init__(self, agent_mode: str = "react_stock"):
        self.agent_mode = agent_mode
        self._logger = None
        self._memory = None
        self._skill_register = None

    def _init_context(self):
        """初始化运行上下文（复用 bootstrap 逻辑）"""
        from utils.bootstrap_common import init_common
        result = init_common()
        self._skill_register = result.skill_register

        logger, _, _, _ = get_logger("eval", skip_db=True)
        self._logger = logger
        # 惰性导入：utils.memory 依赖 langchain，避免拖慢/阻断模块加载
        from utils.memory import MemoryManager
        self._memory = MemoryManager(logger)
    def run_single(
        self,
        benchmark_case: Dict,
    ) -> Dict:
        """
        执行单个 benchmark case（同步接口）
        内部用 asyncio.run() 调用异步 Agent
        有 turns_json 多轮脚本时在同一 dialog 上下文（同一 memory 实例）逐轮执行
        返回运行数据（用于评测）
        """
        if self._logger is None:
            self._init_context()

        turns = parse_case_turns(benchmark_case)
        if not turns:
            return {
                "benchmark_id": benchmark_case["id"],
                "status": "error",
                "error": "case 无可用轮次（question 与 turns_json 均为空）",
                "agent_response": "",
                "tool_calls": [],
                "trace_run_id": None,
                "elapsed_seconds": 0,
            }

        agent = AgentFactory.get(self.agent_mode)
        agent.on_startup()

        registry = SkillRegistry(self._logger, self._memory, self._skill_register)

        turn_results = []
        for turn in turns:
            data = self._execute_turn(agent, registry, turn)
            turn_results.append(data)
            if data["status"] == "error":
                # 对话上下文已断，后续轮次无意义，提前终止
                break

        return self._assemble(benchmark_case, turn_results)

    def _execute_turn(self, agent, registry, turn: Dict) -> Dict:
        """在当前对话上下文执行一轮，返回该轮运行数据（不聚合）。"""
        trace_run_id = None

        # 包装 progress_callback 以捕获 trace_run_id
        def _capture_trace(event):
            nonlocal trace_run_id
            try:
                recorder = _active_recorder.get()
                if recorder and recorder._root:
                    trace_run_id = recorder._root
            except Exception:
                pass

        start_time = time.time()

        # 已在运行中的事件循环内（如 Web 后台线程）时 asyncio.run 会失败，
        # 预检测后用独立 loop 兜底；agent 自身抛出的 RuntimeError 不再误触发重跑
        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        # 运行 Agent（同步调用异步接口）
        try:
            if has_running_loop:
                import asyncio as _aio
                loop = _aio.new_event_loop()
                try:
                    response = loop.run_until_complete(agent.run(
                        turn["question"],
                        registry,
                        self._memory,
                        self._logger,
                        progress_callback=_capture_trace,
                    ))
                finally:
                    loop.close()
            else:
                response = asyncio.run(agent.run(
                    turn["question"],
                    registry,
                    self._memory,
                    self._logger,
                    progress_callback=_capture_trace,
                ))
        except Exception as e:
            return {
                "turn_index": turn["turn_index"],
                "question": turn["question"],
                "answer": "",
                "tool_calls": [],
                "trace_run_id": trace_run_id,
                "iteration_count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "limit_reason": None,
                "elapsed_seconds": time.time() - start_time,
                "status": "error",
                "error": str(e),
            }

        elapsed = time.time() - start_time

        # 如果没捕获到 trace_run_id，尝试从 DB 获取最新
        if not trace_run_id:
            trace_run_id = find_latest_trace_run_id()

        # 从 trace DB 收集工具调用和指标
        tool_calls, run_metrics = self._collect_from_trace(trace_run_id)

        return {
            "turn_index": turn["turn_index"],
            "question": turn["question"],
            "answer": response if isinstance(response, str) else str(response),
            "tool_calls": tool_calls,
            "trace_run_id": trace_run_id,
            "iteration_count": run_metrics.get("iteration_count", 0),
            "tokens_in": run_metrics.get("tokens_in", 0),
            "tokens_out": run_metrics.get("tokens_out", 0),
            "limit_reason": run_metrics.get("limit_reason"),
            "elapsed_seconds": elapsed,
            "status": "success",
            "error": None,
        }

    def _assemble(self, benchmark_case: Dict, turn_results: List[Dict]) -> Dict:
        """把逐轮结果折成 run_data。

        单轮时键形状与历史保持一致；多轮额外附加 `turns` 列表
        （供跨轮取证与后续按轮评测消费）。
        """
        errored = [t for t in turn_results if t["status"] == "error"]
        tool_calls = [c for t in turn_results for c in t["tool_calls"]]
        data = {
            "benchmark_id": benchmark_case["id"],
            "status": "error" if errored else "success",
            "agent_response": "\n\n".join(t["answer"] for t in turn_results if t["answer"]),
            "tool_calls": tool_calls,
            "trace_run_id": next(
                (t["trace_run_id"] for t in turn_results if t["trace_run_id"]), None),
            "iteration_count": sum(t["iteration_count"] for t in turn_results),
            "tool_calls_count": len(tool_calls),
            "tokens_in": sum(t["tokens_in"] for t in turn_results),
            "tokens_out": sum(t["tokens_out"] for t in turn_results),
            "elapsed_seconds": sum(t["elapsed_seconds"] for t in turn_results),
            "limit_reason": next(
                (t["limit_reason"] for t in reversed(turn_results) if t.get("limit_reason")), None),
            "error": errored[0]["error"] if errored else None,
        }
        if len(turn_results) > 1:
            data["turns"] = turn_results
        return data

    def _collect_from_trace(self, trace_run_id: str | None) -> tuple[List[Dict], Dict]:
        """
        从 trace DB 收集工具调用记录和运行指标
        返回 (tool_calls, run_metrics)
        """
        tool_calls = []
        metrics = {
            "iteration_count": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "limit_reason": None,
        }

        if not trace_run_id:
            return tool_calls, metrics

        try:
            from utils.agent_trace.store import store
            conn = store.conn_for_run(trace_run_id)
            if not conn:
                return tool_calls, metrics

            # 查询 tool 类型的 steps
            rows = conn.execute("""
                SELECT step_name, input, output, status, extra
                FROM steps
                WHERE run_id = ? AND step_type = 'tool'
                ORDER BY id
            """, (trace_run_id,)).fetchall()

            for row in rows:
                try:
                    extra = json.loads(row["extra"]) if row["extra"] else {}
                except Exception:
                    extra = {}
                tool_calls.append({
                    "name": row["step_name"] or "",
                    "input": row["input"],
                    "output": row["output"],
                    "success": row["status"] == "success",
                    "extra": extra,
                })

            # 查询 LLM steps 的 token 用量
            llm_rows = conn.execute("""
                SELECT extra FROM steps
                WHERE run_id = ? AND step_type = 'llm' AND extra IS NOT NULL
            """, (trace_run_id,)).fetchall()

            total_in = 0
            total_out = 0
            for row in llm_rows:
                try:
                    extra = json.loads(row["extra"])
                    total_in += extra.get("token_usage", {}).get("input_tokens", 0)
                    total_out += extra.get("token_usage", {}).get("output_tokens", 0)
                except Exception:
                    pass

            metrics["tokens_in"] = total_in
            metrics["tokens_out"] = total_out
            metrics["iteration_count"] = len(llm_rows)

        except Exception as e:
            if self._logger:
                self._logger.debug("E", f"[AgentRunner] 收集 trace 数据失败: {e}")
        return tool_calls, metrics

    def run_batch(
        self,
        cases: List[Dict],
        on_progress: Optional[callable] = None,
    ) -> List[Dict]:
        """批量执行（串行，避免并发问题）"""
        results = []
        for i, case in enumerate(cases):
            try:
                data = self.run_single(case)
                results.append(data)
            except Exception as e:
                results.append({
                    "benchmark_id": case["id"],
                    "status": "error",
                    "error": str(e),
                    "agent_response": "",
                    "tool_calls": [],
                })
            if on_progress:
                on_progress(i + 1, len(cases))
        return results
