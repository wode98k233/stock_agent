"""PDOR Report Enhance 节点（委托 shared ReportEnhanceNode）"""
from agents.shared.report_enhance_node import ReportEnhanceNode


class PdorReportEnhanceNode(ReportEnhanceNode):
    """PDOR 模式报告增强"""
    AGENT_NAME = "pdor"

    def extract_step_results(self, state: dict) -> list:
        plan_steps = state.get("plan_steps", [])
        return [
            {"step": s["step"], "skill": s.get("skill", ""), "purpose": s.get("purpose", ""), "result": s.get("result", "")}
            for s in plan_steps if s.get("status") == "success" and s.get("result")
        ]

    def extract_raw_result(self, state: dict, step_results: list) -> str:
        response = state.get("info_accumulator", "").strip()
        if not response:
            response = state.get("response", "")
        if not response:
            plan_steps = state.get("plan_steps", [])
            successful = [s for s in plan_steps if s.get("status") == "success" and s.get("result")]
            if successful:
                response = "\n\n".join([
                    f"### Step {s['step']}: {s['purpose']}\n{s['result']}"
                    for s in successful
                ])
            else:
                response = "分析过程中未获取到有效数据。"
        return response

    def _fallback_result(self, state: dict) -> dict:
        # PDOR 失败时也返回 response（保持原始行为）
        raw = self.extract_raw_result(state, [])
        return {"response": raw}

    async def __call__(self, state: dict, ctx=None, run_config=None) -> dict:
        # 进度上报
        if ctx and ctx.progress_reporter:
            ctx.progress_reporter.final()
        return await super().__call__(state, ctx, run_config)


# 向后兼容：保留函数形式的接口
async def report_enhance_node(state, ctx, run_config=None) -> dict:
    node = PdorReportEnhanceNode()
    return await node(state, ctx, run_config)
