"""报告与仪表盘（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _ReportMixin:
    REPORT_ENABLE_ANALYSIS_ENGINE = os.getenv("REPORT_ENABLE_ANALYSIS_ENGINE", "true").lower() == "true"

    # 长报告 SSE 兜底时长（只兜「任务卡死」；终态会提前收流）。默认 30 分钟。
    DEEP_REPORT_SSE_MAX_SECONDS = int(os.getenv("DEEP_REPORT_SSE_MAX_SECONDS", "1800"))

    REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "standard")
    # 模板续问判定模式：keyword=基于历史的规则续问(默认,零成本)；llm=调用 LLM 判定续问并选模板

    REPORT_TEMPLATE_CONTINUATION_MODE = os.getenv("REPORT_TEMPLATE_CONTINUATION_MODE", "keyword").strip().lower()

    REPORT_TIME_HORIZON = os.getenv("REPORT_TIME_HORIZON", "short")

    REPORT_MODE = os.getenv("REPORT_MODE", "fast")

    REPORT_MIN_TOKENS = int(os.getenv("REPORT_MIN_TOKENS", "1500"))

    REPORT_TECH_PANEL_MAX = int(os.getenv("REPORT_TECH_PANEL_MAX", "3"))

    REPORT_MAX_TOOL_OUTPUT_CHARS = int(os.getenv("REPORT_MAX_TOOL_OUTPUT_CHARS", "5000"))

    REPORT_MAX_NEWS_CHARS = int(os.getenv("REPORT_MAX_NEWS_CHARS", "1500"))

    REPORT_DRILLDOWN_ENABLED = os.getenv("REPORT_DRILLDOWN_ENABLED", "true").lower() == "true"

    REPORT_DRILLDOWN_MIN_TOOLS = int(os.getenv("REPORT_DRILLDOWN_MIN_TOOLS", "8"))

    REPORT_DRILLDOWN_MIN_CHARS = int(os.getenv("REPORT_DRILLDOWN_MIN_CHARS", "12000"))

    DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() == "true"

    DASHBOARD_LLM_ENABLED = os.getenv("DASHBOARD_LLM_ENABLED", "true").lower() == "true"

    DASHBOARD_MAX_LLM_TOKENS = int(os.getenv("DASHBOARD_MAX_LLM_TOKENS", "2000"))
    # ── LLM 分端点（PRD-A11 FR-2：上下文压缩与工具输出压缩拆开）─────────
    # 上下文压缩：留空 = 跟随主模型。fork 档必须同 provider（缓存池不共享）。

