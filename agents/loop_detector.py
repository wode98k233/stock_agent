"""通用循环检测器

检测 LLM agent 是否陷入重复调用循环。
当连续 N 轮的所有 tool_calls 都能在 exec_state 中找到匹配结果时，
判定为循环，触发强制总结。

适用于 ReAct、Plan 等所有 agent 模式。
"""


class LoopDetector:
    """通用循环检测器：检测 agent 是否陷入重复调用循环"""

    def __init__(self, consecutive_threshold: int = 2):
        self.consecutive_threshold = consecutive_threshold
        self._consecutive_duplicate_count = 0

    def check(self, tool_calls: list[dict], exec_state) -> bool:
        """检查当前轮的 tool_calls 是否全部为重复调用。

        重复定义：find_reusable_tool_call 能在 exec_state 中找到精确或语义匹配的结果。
        返回 True 表示检测到循环，应强制总结。
        """
        # 延迟导入，避免模块级循环依赖。
        from agents.react.tool_reuse import find_reusable_tool_call

        if not tool_calls:
            self._consecutive_duplicate_count = 0
            return False

        all_duplicate = True
        for tc in tool_calls:
            found = find_reusable_tool_call(exec_state, tc.get("name", ""), tc.get("args", {}))
            if found is None:
                all_duplicate = False
                break

        if all_duplicate:
            self._consecutive_duplicate_count += 1
        else:
            self._consecutive_duplicate_count = 0

        return self._consecutive_duplicate_count >= self.consecutive_threshold
