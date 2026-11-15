"""分端点副调用 LLM（压缩/报告/Judge 专用端点）（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _SideLLMMixin:
    REPORT_LLM_MODEL = os.getenv("REPORT_LLM_MODEL", "")

    REPORT_LLM_API_KEY = os.getenv("REPORT_LLM_API_KEY", "")

    REPORT_LLM_API_BASE = os.getenv("REPORT_LLM_API_BASE", "")
    # 报告证据下钻：索引→按序号选择→只载入选中工具全文（默认 true，聚焦注意力）

    CONTEXT_SUMMARY_LLM_MODEL = os.getenv("CONTEXT_SUMMARY_LLM_MODEL", "")

    CONTEXT_SUMMARY_LLM_API_KEY = os.getenv("CONTEXT_SUMMARY_LLM_API_KEY", "")

    CONTEXT_SUMMARY_LLM_API_BASE = os.getenv("CONTEXT_SUMMARY_LLM_API_BASE", "")

    # 压缩形态：2026-10-17 固化为 fork（压缩并入主模型 provider，缓存池共享；
    # sensenova valid_until=2026-12-31 前已在真实链路验证）。standalone 回退档已删，
    # 应急改这一行；react/context.py 仍读此属性，单测可 monkeypatch 走 standalone 路径。
    CONTEXT_SUMMARY_MODE = "fork"

    TOOL_COMPRESS_LLM_MODEL = os.getenv("TOOL_COMPRESS_LLM_MODEL", "")

    TOOL_COMPRESS_LLM_API_KEY = os.getenv("TOOL_COMPRESS_LLM_API_KEY", "")

    TOOL_COMPRESS_LLM_API_BASE = os.getenv("TOOL_COMPRESS_LLM_API_BASE", "")
    # 旧 COMPRESS_LLM_* 兼容别名已于 2026-10-17 删除（发布周期已过；
    # 工具输出压缩用 TOOL_COMPRESS_LLM_*，上下文压缩 fork 跟随主模型）
    # 滑动窗口软闸门：0 = 不启用（默认关闭，灰度期显式打开）

    JUDGE_LLM_MODEL = os.getenv("JUDGE_LLM_MODEL", "")

    JUDGE_LLM_API_KEY = os.getenv("JUDGE_LLM_API_KEY", "")

    JUDGE_LLM_API_BASE = os.getenv("JUDGE_LLM_API_BASE", "")

