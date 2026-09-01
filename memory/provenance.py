"""
记忆系统 — 来源血统（v2 M1）

ProvenanceBuilder: 在 archive() 时从 exec_state 提取工具调用、LLM token 用量、
  trace_run_id、推理路径摘要，构建 provenance dict 存入记忆条目。

ProvenanceQuerier: 从 trace 系统反查完整决策链，供前端"为什么这么判断"按钮使用。

数据流:
  agent 执行 → exec_state.tool_calls / finish_reason → ProvenanceBuilder.build()
  → MemoryEntry.provenance → FTS5 / ChromaDB 存储
  → 前端调 /api/memory/{entry_id}/provenance → ProvenanceQuerier.get_decision_chain()
"""
import logging
from typing import List, Dict, Optional, Any

from memory.context import current_trace_run_id

_log = logging.getLogger(__name__)


class ProvenanceBuilder:
    """从 agent 执行状态构建来源血统 dict。

    在 archive() 归档时调用，不新增 LLM 调用。
    """

    @classmethod
    def build(cls,
              exec_state=None,
              dash_meta: Optional[dict] = None,
              reasoning_summary: str = "",
              ) -> dict:
        """构建 provenance dict。

        Args:
            exec_state: ExecutionState 实例（agents/executor_callbacks.py）
            dash_meta: 仪表盘 metadata dict
            reasoning_summary: 推理路径摘要文本（从 agent 已有输出截取，≤500 字）

        Returns:
            dict: {
                "tool_calls": [{"name": str, "input_keys": [...], "output_keys": [...]}],
                "llm_tokens": {"prompt": int, "completion": int},
                "trace_run_id": str,
                "reasoning_summary": str,
            }
        """
        provenance: Dict[str, Any] = {
            "tool_calls": [],
            "llm_tokens": {},
            "trace_run_id": current_trace_run_id.get() or "",
            "reasoning_summary": "",
        }

        # 1. 工具调用提取
        if exec_state is not None:
            try:
                tool_calls = getattr(exec_state, "tool_calls", None) or []
                seen = set()
                for tc in tool_calls[:10]:  # 最多 10 个
                    name = getattr(tc, "name", "") or getattr(tc, "tool_name", "")
                    tool_input = getattr(tc, "tool_input", {}) or {}
                    output = getattr(tc, "output", "") or ""
                    if not name:
                        continue
                    if name in seen:
                        continue
                    seen.add(name)

                    # 提取 input 关键 key（非空）
                    input_keys = []
                    if isinstance(tool_input, dict):
                        input_keys = [k for k, v in tool_input.items() if v]

                    # 提取 output 关键 key（尝试 JSON 解析）
                    output_keys = []
                    if isinstance(output, str) and output.strip():
                        try:
                            import json
                            parsed = json.loads(output.strip())
                            if isinstance(parsed, dict):
                                output_keys = list(parsed.keys())[:5]
                        except (json.JSONDecodeError, ValueError):
                            output_keys = ["text"]

                    provenance["tool_calls"].append({
                        "name": name,
                        "input_keys": input_keys[:5],
                        "output_keys": output_keys[:5],
                    })

                # 2. LLM token 用量
                tokens = getattr(exec_state, "llm_tokens", None) or {}
                if tokens:
                    provenance["llm_tokens"] = {
                        "prompt": tokens.get("prompt_tokens", 0),
                        "completion": tokens.get("completion_tokens", 0),
                    }
            except Exception:
                _log.debug("[provenance] build from exec_state failed", exc_info=True)

        # 3. reasoning_summary — 截取 ≤500 字
        if reasoning_summary:
            provenance["reasoning_summary"] = reasoning_summary[:500]

        # 4. 仪表盘补充
        if dash_meta and isinstance(dash_meta, dict):
            # 如果还没 reasoning_summary，从仪表盘 key_points 生成
            if not provenance["reasoning_summary"]:
                key_points = dash_meta.get("key_points") or dash_meta.get("key_findings") or []
                if key_points:
                    provenance["reasoning_summary"] = "；".join(str(kp) for kp in key_points[:5])[:500]

        return provenance


class ProvenanceQuerier:
    """从 trace 系统反查完整决策链。

    需要 agent_trace.db 可用（由 trace 系统在 agent 执行时写入）。
    """

    def __init__(self, db_path: str = ""):
        self._db_path = db_path

    def get_decision_chain(self, trace_run_id: str) -> Optional[dict]:
        """按 trace_run_id 反查完整决策链。

        Returns:
            dict: {
                "tool_calls": [...],     # 工具调用列表（name + input + output 摘要）
                "llm_calls": [...],      # LLM 调用列表（role + content 摘要）
                "reasoning_path": str,   # 推理路径文本
            }
            若 trace 不可用或 run_id 不存在，返回 None。
        """
        if not trace_run_id:
            return None

        try:
            from utils.agent_trace.db_adapter import get_decision_chain as _get_chain
            return _get_chain(trace_run_id)
        except ImportError:
            _log.debug("[provenance] agent_trace.db_adapter 不可用")
            return None
        except Exception:
            _log.warning("[provenance] get_decision_chain failed for run=%s", trace_run_id, exc_info=True)
            return None

    def get_provenance_display(self, entry_provenance: dict) -> dict:
        """将存储的 provenance + trace 反查结果合并为前端可用展示。

        Returns:
            dict: {
                "tool_calls": [...],
                "llm_tokens": {...},
                "trace_run_id": str,
                "reasoning_summary": str,
                "full_chain": {...} or null,  # 从 trace 反查的完整信息
            }
        """
        result = dict(entry_provenance) if entry_provenance else {}
        trace_run_id = result.get("trace_run_id", "")
        if trace_run_id:
            chain = self.get_decision_chain(trace_run_id)
            result["full_chain"] = chain
        else:
            result["full_chain"] = None
        return result
