"""对话评审端到端（真实 LLM，谨慎运行）。

覆盖完整链路：真实 dialog（stock_radar.db）→ DialogSource 取轮次 →
逐轮复用/评审 run → dialog 综合评审 → 落库 reviews（scope='dialog'）终态 done。

运行：python -m pytest test/e2e/test_dialog_review_e2e.py -v -m e2e
前置：.env 配置可用 LLM key；db/stock_radar.db 至少一个带 trace_run_id 的对话。

成本控制：自动挑选"带 trace 的轮次最少"的对话；若最少轮次 > 3 则跳过。
"""
import pytest

pytestmark = pytest.mark.e2e


def _has_llm_key() -> bool:
    from config import Config
    return bool(Config.OPENAI_API_KEY)


def _pick_dialog_with_fewest_runs(storage, max_runs: int = 3):
    """选带 trace 轮次最少的对话，控制 LLM 调用量（N+1 次）。"""
    from server.review_service import WebDialogSource

    source = WebDialogSource(storage)
    best = None
    for dialog in storage.list_dialogs(limit=100):
        turns = source.list_turns(dialog["dialog_uuid"])
        runs = [t for t in turns if (t.get("run_id") or "").strip()]
        if not runs:
            continue
        if best is None or len(runs) < len(best[1]):
            best = (dialog["dialog_uuid"], runs)
            if len(runs) == 1:
                break
    if best is None:
        return None
    if len(best[1]) > max_runs:
        return None
    return best


def test_dialog_review_e2e_real_llm():
    if not _has_llm_key():
        pytest.skip("未配置 LLM key（OPENAI_API_KEY）")

    from server.review_service import WebDialogSource
    from server.storage import WebStorage
    from utils.agent_trace.db import get_trace_conn
    from eval.review import engine

    storage = WebStorage()
    picked = _pick_dialog_with_fewest_runs(storage)
    if not picked:
        pytest.skip("没有轮次 <= 3 且带 trace_run_id 的对话（或无可用对话）")
    dialog_uuid, runs = picked

    conn = get_trace_conn()
    assert conn is not None, "agent_trace.db 不可用"

    out = engine.review_dialog(conn, dialog_uuid, WebDialogSource(storage))

    assert out is not None
    assert out["status"] == "done", f"评审未完成: {out.get('error')}"
    assert 0.0 <= out["overall"] <= 100.0
    assert out["rubric_version"] == "v1"
    assert out["scores"], "综合评审应有维度分数"
    inputs = out["inputs"] or {}
    assert inputs.get("dialog_uuid") == dialog_uuid
    assert inputs.get("turns"), "应记录逐轮评审映射"


def test_dialog_review_status_queryable_e2e():
    """评审后可通过服务层查询到版本链与逐轮链路（只读，无 LLM 调用）。"""
    if not _has_llm_key():
        pytest.skip("未配置 LLM key（OPENAI_API_KEY）")

    from server import review_service
    from server.storage import WebStorage

    storage = WebStorage()
    picked = _pick_dialog_with_fewest_runs(storage)
    if not picked:
        pytest.skip("没有轮次 <= 3 且带 trace_run_id 的对话")
    dialog_uuid, _ = picked

    data = review_service.get_dialog_reviews(storage, dialog_uuid)
    assert data["dialog_uuid"] == dialog_uuid
    assert data["runs"], "应返回逐轮链路"
    # 若第一个用例已跑过，则能查到 dialog 级版本链
    assert isinstance(data["dialog_reviews"], list)
