"""共享编排函数 — Plan/PDOR/Unified 共用

原 agents/plan/shared.py 迁移至此。
"""
import json

from agents.prompts import SUMMARY_ON_ERROR_PROMPT


async def stream_and_collect(app, inputs, config, last_state=None):
    """Agent 流式执行并收集最终 response

    inputs=None 时从 checkpoint 恢复（不传 inputs，LangGraph 自动加载上次 checkpoint）。

    注意：始终从 last_state 读取最终 response，而非在 replanner 节点提前捕获。
    这样 report_enhance 和 dashboard 节点的增强结果不会被丢弃。
    """
    if last_state is None:
        last_state = inputs.copy() if inputs else {}

    async for event in app.astream(inputs, config=config):
        for node_name, node_output in event.items():
            if isinstance(node_output, dict):
                last_state.update(node_output)

    return last_state.get("response") or ""


def generate_summary(user_input, user_constraints, past_steps, key_data, logger):
    """在错误恢复时根据已完成步骤生成总结

    从 plan_solve.py._generate_summary 提取。
    """
    from utils.llm_factory import get_llm, tracked_invoke

    llm = get_llm()
    prompt = SUMMARY_ON_ERROR_PROMPT.format(
        user_input=user_input,
        user_constraints=user_constraints if user_constraints else "无",
        past_steps=chr(10).join([f"- {desc}: {res}" for desc, res in past_steps]),
        key_data=json.dumps(key_data, ensure_ascii=False, indent=2) if key_data else "无",
    )
    summary_prompt = [("user", prompt)]

    resp = tracked_invoke(llm, summary_prompt, logger, "summary-on-error", skip_cache_prefix=True)
    return resp.content if hasattr(resp, "content") else str(resp)
