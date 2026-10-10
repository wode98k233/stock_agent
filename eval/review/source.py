"""对话数据源接口与轮次提取（纯函数，不依赖 server）。"""
from __future__ import annotations

from typing import Protocol


class DialogSource(Protocol):
    """dialog 取数接口：由主 web（WebStorage）实现，引擎不反向依赖 server。

    契约（实现方必须遵守，引擎不会二次折叠）：
    - ``get_dialog(dialog_uuid)``：返回 WebStorage.get_dialog 的行（至少含
      ``dialog_uuid/title``）；dialog 不存在时返回 None。
    - ``list_turns(dialog_uuid)``：返回 ``build_turns`` 形状且按对话时间升序的
      轮次列表，键固定为 ``turn_index/question/answer/run_id/created_at``；
      实现方必须自行把原始消息折叠成该形状。
    """

    def get_dialog(self, dialog_uuid: str) -> dict | None: ...

    def list_turns(self, dialog_uuid: str) -> list[dict]: ...


def build_turns(messages: list[dict]) -> list[dict]:
    """把 web_messages 折叠成 [{turn_index, question, answer, run_id, created_at}]。

    user 消息开一轮；紧随其后的 assistant 消息闭合该轮。连续 user 消息时，
    前一条以空 answer 保留；没有 assistant 的尾轮同样保留。

    字段口径：
    - ``turn_index`` 从 1 开始；
    - ``created_at`` 优先取 assistant 消息的时间，缺失时回落 question 的时间；
    - ``trace_run_id`` 为 None/空串时 ``run_id=""``。
    """
    turns: list[dict] = []

    def _close(question_msg: dict, answer_msg: dict | None = None) -> None:
        answer = answer_msg or {}
        turns.append({
            "turn_index": len(turns) + 1,
            "question": question_msg.get("content") or "",
            "answer": answer.get("content") or "",
            "run_id": answer.get("trace_run_id") or "",
            "created_at": answer.get("created_at")
            or question_msg.get("created_at") or "",
        })

    pending: dict | None = None
    for msg in messages or []:
        role = msg.get("role")
        if role == "user":
            if pending is not None:
                _close(pending)
            pending = msg
        elif role == "assistant" and pending is not None:
            _close(pending, msg)
            pending = None
    if pending is not None:
        _close(pending)
    return turns
