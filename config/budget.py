"""预算控制（token / 调用次数 / 时长 / 计费口径）（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _BudgetMixin:
    GROUP_MAX_LLM_CALLS = int(os.getenv("GROUP_MAX_LLM_CALLS", "100"))

    # 预算控制

    MAX_TOKENS_PER_QUERY = int(os.getenv("MAX_TOKENS_PER_QUERY", "50000"))

    MAX_LLM_CALLS_PER_QUERY = int(os.getenv("MAX_LLM_CALLS_PER_QUERY", "30"))

    MAX_TIME_SECONDS = int(os.getenv("MAX_TIME_SECONDS", "600"))
    # 豁免窗口：预算超限后用户选择继续时，短期内跳过 budget check

    BUDGET_EXEMPT_CALLS_LIMIT = int(os.getenv("BUDGET_EXEMPT_CALLS_LIMIT", "10"))

    BUDGET_EXEMPT_TIME_LIMIT = int(os.getenv("BUDGET_EXEMPT_TIME_LIMIT", "120"))

    # 缓存（小时）

    BUDGET_CACHE_HIT_WEIGHT = float(os.getenv("BUDGET_CACHE_HIT_WEIGHT", "0") or 0)
    # 免费端点权重：0 = 完全不占预算

    BUDGET_FREE_WEIGHT = float(os.getenv("BUDGET_FREE_WEIGHT", "0") or 0)
    # 原始 token 硬上限（0 = 不启用）

    MAX_RAW_TOKENS_PER_QUERY = int(os.getenv("MAX_RAW_TOKENS_PER_QUERY", "0") or 0)
    # 工具输出压缩：承接原 COMPRESS_LLM_*（sensenova 免费端点），本期行为不变

