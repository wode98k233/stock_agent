"""数据缓存 / Checkpoint / 日志 / 调试（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _InfraMixin:
    CACHE_EXPIRE_HOURS = int(os.getenv("CACHE_EXPIRE_HOURS", "24"))

    CACHE_STOCK_HISTORY_HOURS = int(os.getenv("CACHE_STOCK_HISTORY_HOURS", "24"))

    CACHE_BOARD_HOURS = int(os.getenv("CACHE_BOARD_HOURS", "24"))

    CACHE_NEWS_HOURS = int(os.getenv("CACHE_NEWS_HOURS", "2"))

    CACHE_RATING_HOURS = int(os.getenv("CACHE_RATING_HOURS", "72"))

    CACHE_FINANCIAL_HOURS = int(os.getenv("CACHE_FINANCIAL_HOURS", "48"))

    # 日志

    LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

    # 调试模式：在 DEBUG 级别下，每个 step 执行前需要手动确认

    DEBUG_STEP_CONFIRM = os.getenv("DEBUG_STEP_CONFIRM", "true").lower() == "true"

    # 调用链追踪

    CHECKPOINT_BACKEND = os.getenv("CHECKPOINT_BACKEND", "memory")

    CHECKPOINT_PG_URI = os.getenv("CHECKPOINT_PG_URI", "")

    # 东财反扒补丁

