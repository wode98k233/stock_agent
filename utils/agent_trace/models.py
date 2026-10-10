import functools
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _collect_migrations(table_defs):
    """把 TableDef.migrate 归集为迁移函数列表（与 registry._with_migrate 同口径）。"""
    migs = []
    for td in table_defs:
        if td.migrate:
            migs.extend(td.migrate if isinstance(td.migrate, (list, tuple)) else [td.migrate])
    return migs


def _ensure_tables(conn, table_defs) -> None:
    """把 conn 对齐到给定表定义（幂等加性）。"""
    from utils.db_schema.migrator import ensure_schema
    ensure_schema(conn, table_defs, _collect_migrations(table_defs))


def ensure_month_schema(conn) -> None:
    """月库表结构：runs/steps/messages（分库后月库不含 reviews）。"""
    from utils.db_schema.tables.agent_trace import MONTH_TABLES
    _ensure_tables(conn, MONTH_TABLES)


def ensure_reviews_schema(conn) -> None:
    """主库表结构：仅 reviews。"""
    from utils.db_schema.tables.agent_trace import REVIEW_TABLES
    _ensure_tables(conn, REVIEW_TABLES)


def ensure_full_schema(conn) -> None:
    """单文件全 4 表结构（显式路径兼容：测试/导出导入工具/迁移前旧库）。"""
    from utils.db_schema.tables.agent_trace import TABLES
    _ensure_tables(conn, TABLES)


# 兼容别名：既有调用点（core/db_adapter/测试）引用的旧名
ensure_trace_schema = ensure_full_schema


@contextmanager
def _connect(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_db(db: Path, full: bool = True):
    """建库文件并 ensure 表结构（幂等）。

    full=True 全 4 表（显式单文件路径：测试/导出导入工具）；
    full=False 月库 3 表（runs/steps/messages，分库默认路由）。
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_full_schema(conn) if full else ensure_month_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except Exception:
        return str(obj)


def _pick_name(ser: dict) -> str:
    if not ser:
        return "unknown"
    if ser.get("name"):
        return ser["name"]
    sid = ser.get("id")
    if isinstance(sid, list) and sid:
        return sid[-1]
    if isinstance(sid, str):
        return sid
    kw = ser.get("kwargs", {})
    return kw.get("model_name") or kw.get("model") or "unknown"


def _msg_info(msg) -> dict:
    info = dict(
        role=getattr(msg, "type", "unknown"),
        content=getattr(msg, "content", ""),
        tool_calls=None,
        tool_call_id=getattr(msg, "tool_call_id", None),
        reasoning=None,
    )
    tc = getattr(msg, "tool_calls", None)
    if tc:
        info["tool_calls"] = json.dumps(tc, ensure_ascii=False, default=str)
    # 思考过程（reasoning_content）截断存储，避免撑爆 trace 库
    ak = getattr(msg, "additional_kwargs", None) or {}
    reasoning = ak.get("reasoning_content")
    if reasoning is not None:
        info["reasoning"] = str(reasoning)[:2000]
    return info


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            logger.error("[TraceRecorder] %s error: %s", fn.__name__, exc, exc_info=True)
    return wrapper


_GRAPH_NODE_NAMES = {
    "agent": "agent",
    "tools": "tools",
    "planner": "planner",
    "executor": "executor",
    "replanner": "replanner",
    "classifier": "classifier",
    "skill-selector": "skill-selector",
    "select_template": "select_template",
    "template_report": "template_report",
    "partial_summary": "partial_summary",
    "dashboard": "dashboard",
    "unified_executor": "unified_executor",
    "observer": "observer",
    "adjuster": "adjuster",
    "reviewer": "reviewer",
    "react-context-summary": "react-context-summary",
    "react-tool-compress": "react-tool-compress",
    "sentiment-extract": "sentiment-extract",
}

_GRAPH_NODE_LABELS = {
    "classifier": "问题分类",
    "skill-selector": "技能选择",
    "select_template": "模板选择",
    "template_report": "模板报告",
    "partial_summary": "部分总结",
    "dashboard": "仪表盘",
    "planner": "生成计划",
    "executor": "执行步骤",
    "replanner": "重新规划",
    "observer": "观察判定",
    "adjuster": "调整计划",
    "reviewer": "总结复盘",
    "agent": "Agent 思考",
    "tools": "工具执行",
    "unified_executor": "统一执行",
    "react-context-summary": "上下文压缩",
    "react-tool-compress": "工具输出压缩",
    "sentiment-extract": "舆情分析",
}

_TYPE_ICONS = {
    "llm": "🤖", "tool": "🔧", "chain": "🔗", "retriever": "📄",
    "agent": "🤖", "tools": "🔧", "planner": "📋", "executor": "⚡",
    "replanner": "🔄", "observer": "👁️", "adjuster": "🔧", "reviewer": "📝",
    "classifier": "🏷️", "skill-selector": "🎯", "select_template": "📄",
    "template_report": "📊", "partial_summary": "📝", "dashboard": "📈",
    "graph": "🌳", "sentiment-extract": "📰",
    "undefined": "❓",
}


def _identify_graph_node(serialized, kw, parent_run_id=None):
    name = _pick_name(serialized)
    metadata = kw.get("metadata", {})
    node = metadata.get("node", "")
    langgraph_node = metadata.get("langgraph_node", "")
    pdor_context = metadata.get("pdor_context", "")

    # 优先级 1: metadata.node（业务代码显式指定）
    if node:
        display = _GRAPH_NODE_NAMES.get(node, node)
        label = _GRAPH_NODE_LABELS.get(node, display)
        return node, display, label, False

    # 优先级 2: metadata.langgraph_node（LangGraph 自动传入）
    if langgraph_node:
        display = _GRAPH_NODE_NAMES.get(langgraph_node, langgraph_node)
        label = _GRAPH_NODE_LABELS.get(langgraph_node, display)
        return langgraph_node, display, label, False

    if parent_run_id is None:
        return "graph", "LangGraph", "LangGraph 工作流", False

    name_lower = name.lower()

    # 优先级 3: pdor_context == "react_step" 下识别 agent/tools
    if pdor_context == "react_step":
        if "agent" in name_lower:
            return "agent", "agent", "Agent 思考", False
        if "tool" in name_lower:
            return "tools", "tools", "工具执行", False

    # 优先级 4: 旧的名称猜测逻辑
    if "agent" in name_lower:
        return "agent", "agent", "Agent 思考", True
    if "tool" in name_lower:
        return "tools", "tools", "工具执行", True

    # 优先级 5: fallback
    is_noise = parent_run_id is not None and not langgraph_node
    return "undefined", name, name, is_noise
