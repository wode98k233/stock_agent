"""共享步骤执行工具函数 — Plan/PDOR Executor 共用

提取两个 executor 中完全相同的基础设施工具函数。
核心编排逻辑（重试策略、失败处理、状态更新）保留在各 executor 中。
"""
from config import Config
from utils.budget import BudgetExceeded, BudgetExemptWindow


def restore_exempt_window(state: dict) -> BudgetExemptWindow | None:
    """从 state 恢复豁免窗口（Plan/PDOR 完全相同的逻辑）"""
    exempt_data = state.get("budget_exempt")
    if not exempt_data:
        return None
    try:
        window = BudgetExemptWindow.from_dict(exempt_data)
        return window if window.is_active() else None
    except Exception:
        return None


def create_exempt_window(budget) -> BudgetExemptWindow:
    """创建新的豁免窗口并设置到 budget"""
    window = BudgetExemptWindow(
        calls_limit=Config.BUDGET_EXEMPT_CALLS_LIMIT,
        time_limit=Config.BUDGET_EXEMPT_TIME_LIMIT,
    )
    budget.set_exempt_window(window)
    return window


def check_budget_with_exempt(
    state: dict,
    budget,
    logger,
    exempt_window: BudgetExemptWindow | None,
    overrun_limit: int = 3,
) -> tuple[bool, BudgetExemptWindow | None, dict]:
    """统一的预算检查 — 考虑豁免窗口

    Args:
        state: 当前状态
        budget: BudgetController
        logger: 日志器
        exempt_window: 当前豁免窗口
        overrun_limit: 用户允许继续执行的最大次数（0 = 不允许 overrun）

    Returns:
        (可以继续, 更新后的豁免窗口, state 更新 dict)
    """
    # 豁免窗口内跳过检查
    if exempt_window and exempt_window.is_active():
        exempt_window.consume()
        logger.info("B", f"豁免窗口内，跳过 budget check（剩余 {exempt_window.remaining_calls} 次）")
        return True, exempt_window, {}

    # 正常预算检查
    try:
        budget.check(logger)
        return True, exempt_window, {}
    except BudgetExceeded:
        pass

    # 预算超限，检查 overrun 次数
    if overrun_limit > 0:
        overrun_count = state.get("user_approved_overrun_count") or 0
        if overrun_count >= overrun_limit:
            logger.warning("B", f"用户已连续选择继续执行 {overrun_count} 次，强制终止")
            return False, exempt_window, {"_budget_exceeded": True}

    # 询问用户
    from agents.shared.exception_utils import handle_budget_exceeded
    result = handle_budget_exceeded(state, logger)
    if result.get("_user_approved_overrun"):
        new_window = create_exempt_window(budget)
        new_count = (state.get("user_approved_overrun_count") or 0) + 1
        logger.info("B", f"用户选择继续执行（第 {new_count} 次），创建豁免窗口")
        return True, new_window, {
            "budget_exempt": new_window.to_dict(),
            "user_approved_overrun_count": new_count,
        }

    return False, exempt_window, result


class ErrorDeduplicator:
    """错误指纹去重器（支持两种策略）"""

    def __init__(self, strategy: str = "set"):
        """
        Args:
            strategy: "set" = 全历史去重（Plan 用），"last" = 仅比较上一次（PDOR 用）
        """
        self.strategy = strategy
        self._seen: set[str] = set()
        self._last: str = ""

    def is_duplicate(self, error: Exception) -> bool:
        """检查是否为重复错误"""
        from agents.utils import _error_fingerprint
        fp = _error_fingerprint(error)

        if self.strategy == "set":
            if fp in self._seen:
                return True
            self._seen.add(fp)
            return False
        else:  # "last"
            if self._last and fp == self._last:
                return True
            self._last = fp
            return False

    def get_fingerprint(self, error: Exception) -> str:
        """获取错误指纹"""
        from agents.utils import _error_fingerprint
        return _error_fingerprint(error)
