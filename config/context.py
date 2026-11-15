"""上下文压缩与工具输出压缩（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _ContextMixin:
    REACT_ENABLE_CONTEXT_COMPACTION = os.getenv("REACT_ENABLE_CONTEXT_COMPACTION", "true").lower() == "true"

    REACT_CONTEXT_RECENT_ROUNDS = int(os.getenv("REACT_CONTEXT_RECENT_ROUNDS", "0"))

    REACT_SUMMARY_TRIGGER_ROUNDS = int(os.getenv("REACT_SUMMARY_TRIGGER_ROUNDS", "12"))

    REACT_SUMMARY_PENDING_CHARS = int(os.getenv("REACT_SUMMARY_PENDING_CHARS", "100000"))

    REACT_SUMMARY_MAX_CHARS = int(os.getenv("REACT_SUMMARY_MAX_CHARS", "2500"))

    REACT_DEDUP_MODE = os.getenv("REACT_DEDUP_MODE", "exact").lower()

    REACT_CONTEXT_DEBUG_LOG = os.getenv("REACT_CONTEXT_DEBUG_LOG", "true").lower() == "true"

    # Plan / PDOR / Unified 模式配置

    REACT_CONTEXT_SOFT_LIMIT_TOKENS = int(os.getenv("REACT_CONTEXT_SOFT_LIMIT_TOKENS", "0"))
    # 评测 Judge 模型

    TOOL_OUTPUT_COMPRESS_THRESHOLD = int(os.getenv("TOOL_OUTPUT_COMPRESS_THRESHOLD", "3000"))

    TOOL_HARD_TRUNCATE_CHARS = int(os.getenv("TOOL_HARD_TRUNCATE_CHARS", "8000"))

    TOOL_COMPRESS_THRESHOLDS = os.getenv("TOOL_COMPRESS_THRESHOLDS", "")
    # 工具输出规则压缩（默认关闭；policy 按工具配置 off/shadow/on，例如 {"mx_xuangu_filter":"shadow"}）

    TOOL_RULE_COMPRESS_ENABLED = os.getenv("TOOL_RULE_COMPRESS_ENABLED", "false").lower() == "true"

    TOOL_RULE_COMPRESS_POLICY = os.getenv("TOOL_RULE_COMPRESS_POLICY", "")

    INFO_ACCUMULATOR_COMPRESS_THRESHOLD = int(os.getenv("INFO_ACCUMULATOR_COMPRESS_THRESHOLD", "50000"))

    # Checkpoint 后端

