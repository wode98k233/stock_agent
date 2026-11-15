"""共享 Report Enhance 节点 — 统一三种模式的报告增强逻辑

核心调用完全相同：run_report_post_processing()。
差异仅在于从不同 state 格式提取 step_results 和 raw_result。
"""
from config import Config
from utils.budget import BudgetExceeded
from utils.logger import ensure_radar


class ReportEnhanceNode:
    """共享报告增强节点基类（class + __call__ 模式）

    子类只需实现两个提取方法：
    - extract_step_results(state) -> list[dict]
    - extract_raw_result(state, step_results) -> str
    """

    # 子类覆盖
    AGENT_NAME: str = ""

    def extract_step_results(self, state: dict) -> list:
        """从 state 提取 step results 列表"""
        return []

    def extract_raw_result(self, state: dict, step_results: list) -> str:
        """从 state 构建 raw_result 字符串"""
        return ""

    def extract_tool_calls(self, state: dict) -> list:
        """从 state 提取 tool_calls（ReAct/Unified 用）"""
        return []

    def extract_conversation(self, state: dict) -> tuple:
        """提取 (对话消息, 绑定的同一份工具数组)；默认不适用。

        PRD-A9 §14 fork 形态：报告复用 agent 已发热的对话前缀，尾部追加报告指令。
        只有「单个 agent loop 一口气跑完」的模式（ReAct / Unified Plan）才成立——
        plan/pdor/group 的报告输入是跨 step 聚合，单条对话里本就没有这批工具结果。
        保持默认空 → report_builder 自动回落证据注入路径。
        """
        return None, None

    async def __call__(self, state: dict, ctx=None, run_config=None) -> dict:
        """执行报告增强"""
        from agents.common import run_report_post_processing

        logger = ensure_radar(ctx.logger if ctx else None)

        if not Config.REPORT_ENABLE_ANALYSIS_ENGINE:
            return self._fallback_result(state)

        step_results = self.extract_step_results(state)
        raw_result = self.extract_raw_result(state, step_results)
        if not raw_result:
            return self._fallback_result(state)

        tool_calls = self.extract_tool_calls(state)
        conversation, conversation_tools = self.extract_conversation(state)

        # fork_sink：接收 report-fast **实际发出的 payload**，原样带进 state，
        # 供紧随其后的 dashboard 节点复用同一前缀（PRD-A11「报告 → 仪表盘」预热）。
        # 只在没有 agent 对话可 fork 的模式（plan / pdor / group）才有意义。
        fork_sink: dict = {}
        try:
            enhanced = await run_report_post_processing(
                user_input=self._get_user_input(state),
                agent_name=self.AGENT_NAME,
                tool_calls=tool_calls,
                raw_result=raw_result,
                template_id=state.get("template_id", state.get("selected_template_id", Config.REPORT_TEMPLATE)),
                selected_skills=state.get("selected_skills", []),
                logger=logger,
                budget=ctx.budget if ctx else None,
                run_config=run_config,
                step_results=step_results,
                conversation=conversation,
                conversation_tools=conversation_tools,
                fork_sink=fork_sink,
            )
            if enhanced:
                updates = self._success_result(state, enhanced)
                if fork_sink.get("messages") and not conversation:
                    # 报告 payload 已冻结 → 交给 dashboard 当前缀
                    updates["_report_fork_messages"] = list(fork_sink["messages"])
                    updates["_report_fork_tools"] = list(fork_sink.get("tools") or [])
                return updates
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.warning("A", f"报告增强失败，保留原始结果: {e}")

        return self._fallback_result(state)

    def _get_user_input(self, state: dict) -> str:
        """提取 user_input（字段名在不同 state 中不同）"""
        return state.get("user_input", state.get("input", ""))

    def _fallback_result(self, state: dict) -> dict:
        """失败/跳过时的返回值"""
        return {}

    def _success_result(self, state: dict, enhanced: str) -> dict:
        """成功时的返回值"""
        return {"response": enhanced}
