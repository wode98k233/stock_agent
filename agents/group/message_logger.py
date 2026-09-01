"""GroupMessageLogger — 统一 DB 持久化 + SSE 进度推送

每个节点通过此类记录调度交互，避免散落的 save_group_message + progress.report 调用。
"""
from agents.group.messages_db import save_group_message
from utils.progress import ProgressReporter, ProgressType
from utils.logger import ensure_radar


class GroupMessageLogger:
    """Agent Group 消息日志 — 封装 DB 写入与 SSE 事件推送"""

    def __init__(
        self,
        progress: ProgressReporter = None,
        dialog_uuid: str = None,
        task_id: str = None,
        logger=None,
    ):
        self._progress = progress
        self._dialog_uuid = dialog_uuid or ""
        self._task_id = task_id or ""
        self._logger = ensure_radar(logger) if logger else None

    @classmethod
    def from_state(cls, state: dict, progress: ProgressReporter = None, logger=None):
        """从 GroupState 构建实例"""
        return cls(
            progress=progress,
            dialog_uuid=state.get("dialog_uuid") or "",
            task_id=state.get("task_id") or "",
            logger=logger,
        )

    # ── 调度层事件 ──────────────────────────────────────────────

    def plan(self, steps: list):
        """Planner 生成计划"""
        data = [
            {
                "step": s.get("step"),
                "agent_names": s.get("agent_names", []),
                "task_purpose": s.get("task_purpose", ""),
                "run_group": s.get("run_group"),
            }
            for s in steps
        ]
        self._emit(ProgressType.GROUP_PLAN, f"生成执行计划: {len(steps)} 个步骤", data=data)
        self._save(role="dispatcher", msg_type="plan", content=self._format_plan(steps))

    def step_start(self, step: int, agent_names: list, task_purpose: str, parallel: bool = False):
        """Executor 开始调度某个 agent 组"""
        label = ", ".join(agent_names)
        self._emit(
            ProgressType.GROUP_STEP_START,
            f"[Step {step}] {label}: {task_purpose}",
            data={"step": step, "agent_names": agent_names, "parallel": parallel},
        )
        self._save(
            step_index=step, role="dispatcher", msg_type="task",
            agent_name=label, content=task_purpose,
        )

    def step_result(self, step: int, agent_names: list, status: str, result: str, feedback: str = ""):
        """Sub-agent 返回结果"""
        label = ", ".join(agent_names)
        # SSE 推送用截断版，DB 存完整内容
        summary = result[:300] + "..." if len(result) > 300 else result
        self._emit(
            ProgressType.GROUP_STEP_RESULT,
            f"[Step {step}] {label}: {status}",
            data={
                "step": step, "agent_names": agent_names,
                "status": status, "result_summary": summary, "feedback": feedback,
            },
        )
        self._save(
            step_index=step, role="sub_agent", msg_type="result",
            agent_name=label, content=result,
            extra={"status": status, "feedback": feedback},
        )

    def observe(self, step: int, observation: str, reasoning: str = ""):
        """Observer 判定"""
        self._emit(
            ProgressType.GROUP_OBSERVE,
            f"Observer: {observation}",
            data={"step": step, "observation": observation, "reasoning": reasoning},
        )
        self._save(
            role="dispatcher", msg_type="observe",
            content=f"Step {step} {observation}: {reasoning}",
        )

    def adjust(self, added_steps: list):
        """Adjuster 添加步骤"""
        purposes = [s.get("task_purpose", "") for s in added_steps]
        self._emit(
            ProgressType.GROUP_ADJUST,
            f"调整计划: +{len(added_steps)} 步",
            data={"added_steps": purposes},
        )
        self._save(
            role="dispatcher", msg_type="adjust_plan",
            content=f"调整计划新增 {len(added_steps)} 步:\n" + "\n".join(f"- {p}" for p in purposes),
        )

    def replan(self, new_steps: list):
        """Planner 重规划"""
        self._emit(
            ProgressType.GROUP_REPLAN,
            f"重规划: {len(new_steps)} 步",
            data={"step_count": len(new_steps)},
        )
        self._save(
            role="dispatcher", msg_type="replan_plan",
            content=self._format_plan(new_steps),
        )

    def resolve(self, requests: list, resolved_count: int):
        """ResolveDeps 解析依赖"""
        self._emit(
            ProgressType.GROUP_RESOLVE,
            f"解析依赖: {resolved_count} 项",
            data={"request_count": len(requests), "resolved_count": resolved_count},
        )
        self._save(
            role="dispatcher", msg_type="adjust_notice",
            content=f"解析依赖请求: {len(requests)} 项，已满足 {resolved_count} 项",
        )

    def early_stop(self, reason: str):
        """提前终止通知"""
        self._save(role="dispatcher", msg_type="early_stop", content=reason)

    def error(self, message: str):
        """异常通知"""
        self._save(role="dispatcher", msg_type="error", content=message)

    # ── 内部方法 ──────────────────────────────────────────────

    def _emit(self, event_type: ProgressType, message: str, data=None):
        """推送 SSE 进度事件"""
        if self._progress:
            try:
                self._progress.report(event_type, message, data=data)
            except Exception:
                pass

    def _save(self, role: str, msg_type: str, content: str,
              step_index: int = None, agent_name: str = None, extra: dict = None):
        """写入 group_messages 表"""
        if not self._dialog_uuid:
            return
        try:
            save_group_message(
                dialog_uuid=self._dialog_uuid,
                task_id=self._task_id,
                step_index=step_index,
                role=role,
                agent_name=agent_name,
                msg_type=msg_type,
                content=content,
                extra=extra,
            )
        except Exception as e:
            if self._logger:
                self._logger.warning("G", f"保存 group_message 失败: {e}")

    @staticmethod
    def _format_plan(steps: list) -> str:
        """格式化计划为可读文本"""
        lines = []
        for s in steps:
            names = ", ".join(s.get("agent_names", []))
            rg = s.get("run_group", "")
            lines.append(f"{s.get('step', '?')}. [{names}] (组{rg}) {s.get('task_purpose', '')}")
        return "\n".join(lines)
