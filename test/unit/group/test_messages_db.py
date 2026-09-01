import uuid
import pytest
from agents.group.messages_db import (
    init_group_messages_table, save_group_message, get_group_messages,
    get_group_db,
)


@pytest.fixture(autouse=True)
def setup_db():
    init_group_messages_table()
    # 清理历史测试数据
    test_uuids = ["test-dialog", "test-dialog-2", "test-dialog-3"]
    with get_group_db() as conn:
        for du in test_uuids:
            conn.execute("DELETE FROM group_messages WHERE dialog_uuid = ?", (du,))
        conn.commit()
    yield


def test_save_and_get_message():
    save_group_message(
        dialog_uuid="test-dialog",
        task_id="task-001",
        role="dispatcher",
        msg_type="plan",
        content="执行计划：1. 分析技术指标",
    )
    msgs = get_group_messages("test-dialog")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "dispatcher"
    assert msgs[0]["msg_type"] == "plan"


def test_multiple_messages_ordered():
    for i in range(3):
        save_group_message(
            dialog_uuid="test-dialog-2",
            task_id="task-002",
            role="dispatcher" if i % 2 == 0 else "sub_agent",
            msg_type="task" if i % 2 == 0 else "result",
            content=f"消息 {i}",
            step_index=i,
        )
    msgs = get_group_messages("test-dialog-2")
    assert len(msgs) == 3
    assert msgs[0]["content"] == "消息 0"
    assert msgs[2]["content"] == "消息 2"


def test_message_with_extra():
    save_group_message(
        dialog_uuid="test-dialog-3",
        task_id="task-003",
        role="sub_agent",
        msg_type="request_info",
        content="需要板块数据",
        extra={"requests": [{"target": "chain_analyst"}]},
    )
    msgs = get_group_messages("test-dialog-3")
    assert msgs[0]["extra"] is not None
