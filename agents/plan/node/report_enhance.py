"""Plan Agent 报告增强节点（委托 shared ReportEnhanceNode）"""
from agents.shared.report_enhance_node import ReportEnhanceNode


class PlanReportEnhanceNode(ReportEnhanceNode):
    """Plan 模式报告增强

    Plan（多步循环）：step_results 给分析引擎，不传 tool_calls。
    Unified Plan（单步执行）：传 tool_calls 给分析引擎做 slot 提取。
    """
    AGENT_NAME = "plan_solve"

    def extract_step_results(self, state: dict) -> list:
        step_results_raw = state.get("step_results", [])
        past_steps = state.get("past_steps", [])
        results = []
        if step_results_raw:
            for i, sr in enumerate(step_results_raw):
                summary = sr.get("summary", "")
                if summary:
                    results.append({
                        "step": i + 1,
                        "skill": sr.get("skill", ""),
                        "purpose": sr.get("purpose", sr.get("step_purpose", "")),
                        "result": summary,
                    })
        elif past_steps:
            for i, (desc, result) in enumerate(past_steps):
                if result:
                    results.append({
                        "step": i + 1,
                        "skill": "",
                        "purpose": str(desc),
                        "result": str(result),
                    })
        # 区分 Plan/Unified：Unified 有 tool_calls 且只有一条 step
        state_tool_calls = state.get("tool_calls", [])
        is_unified = bool(state_tool_calls) and len(results) == 1
        if is_unified:
            return []  # Unified 模式不传 step_results
        return results

    def extract_raw_result(self, state: dict, step_results: list) -> str:
        response = state.get("response", "")
        parts = [sr["result"] for sr in step_results]
        if parts:
            full = "\n\n".join(parts)
            if response and response not in full:
                full += f"\n\n{response}"
            return full
        return response

    def extract_tool_calls(self, state: dict) -> list:
        state_tool_calls = state.get("tool_calls", [])
        step_results = self.extract_step_results(state)
        is_unified = bool(state_tool_calls) and len(step_results) == 0
        return state_tool_calls if is_unified else []


# 向后兼容：保留函数形式的接口
async def report_enhance_node(state: dict, ctx, run_config=None) -> dict:
    node = PlanReportEnhanceNode()
    return await node(state, ctx, run_config)
