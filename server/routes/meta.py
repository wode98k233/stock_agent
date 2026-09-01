"""元数据路由：/api/modes, /api/datasources, /api/agents, /api/health, /api/system/*"""
import os
import sys
import time as _time
import threading
from time import monotonic
from typing import Any

from fastapi import APIRouter

router = APIRouter()

# ── 服务就绪状态 ──────────────────────────────────────────────
# T0: 进程启动即可用（health/status）
# T1: bootstrap_common() 完成（meta/config/skills）
# T2: agent 预热完成（dialogs/tasks/report）
_SERVER_START_TIME = _time.time()
_readiness_level = "t0_immediate"  # t0_immediate → t1_basic → t2_full
_agent_ready = False

# ── 记忆系统就绪状态 ──
# 记忆启用时，embedding 模型需要冷加载（Ollama 首次拉起 bge-m3 约 1~2 分钟）。
# 在此期间若放行对话，首条请求会被冷加载阻塞；故记忆未就绪时也返回 503，
# 由前端等待气泡 + 进度条引导用户，就绪后自动补发。
_memory_enabled = False   # 记忆系统是否启用（Config.MEMORY_ENABLED）
_memory_ready = False     # 记忆系统是否就绪（embedding 冷加载完成；禁用时恒为 True）
_memory_warming = False   # 是否正在预热（后台加载 embedding 模型）


def set_readiness_basic():
    """标记基础服务就绪（bootstrap_common 完成），T1 接口可用。"""
    global _readiness_level
    _readiness_level = "t1_basic"


def set_readiness_full():
    """标记全部服务就绪（agent 预热完成），T2 接口可用。"""
    global _readiness_level, _agent_ready
    _readiness_level = "t2_full"
    _agent_ready = True


def set_memory_enabled(flag: bool) -> None:
    """标记记忆系统是否启用（启动时由 bootstrap 根据 Config.MEMORY_ENABLED 设置）。"""
    global _memory_enabled
    _memory_enabled = bool(flag)
    # 未启用记忆系统时，视为始终就绪（不阻塞对话），无需等待
    if not _memory_enabled:
        global _memory_ready, _memory_warming
        _memory_ready = True
        _memory_warming = False


def set_memory_warming(flag: bool) -> None:
    """标记记忆系统是否正在预热（后台加载 embedding 模型）。"""
    global _memory_warming
    if _memory_enabled:
        _memory_warming = bool(flag)


def set_memory_ready() -> None:
    """标记记忆系统预热完成（embedding 模型已加载）。"""
    global _memory_ready, _memory_warming
    _memory_ready = True
    _memory_warming = False


def is_memory_ready() -> bool:
    """记忆系统是否就绪。未启用时恒为 True（不阻塞对话）。"""
    if not _memory_enabled:
        return True
    return _memory_ready


def memory_status() -> dict:
    """对外暴露的记忆系统状态。"""
    return {
        "enabled": _memory_enabled,
        "ready": is_memory_ready(),
        "warming": _memory_warming and _memory_enabled,
    }


def is_agent_ready() -> bool:
    return _agent_ready


@router.get("/api/health")
async def health():
    """服务健康检查 + 就绪状态。

    readiness 级别:
      - t0_immediate: 进程已启动，仅 health/status 可用
      - t1_basic:     基础服务就绪，meta/config/skills 可用
      - t2_full:      全部服务就绪，dialogs/tasks 可用
    """
    return {
        "status": "ok",
        "readiness": _readiness_level,
        "agent_ready": _agent_ready,
        "memory_enabled": _memory_enabled,
        "memory_ready": is_memory_ready(),
        "memory_warming": _memory_warming and _memory_enabled,
        "uptime_seconds": round(_time.time() - _SERVER_START_TIME, 1),
    }

DATASOURCE_STATUS_CACHE_SECONDS = 5.0

# 模式标签兜底（agent 类没有 display_name 时使用）
_MODE_LABELS_FALLBACK = {
    "react_stock": ("ReAct", "动态思考和工具调用，适合简单到中等复杂度查询。"),
    "plan_solve": ("Plan & Solve", "先规划再执行，适合复杂分析。"),
    "unified_plan": ("Unified Plan", "单次统一执行计划，适合结构清晰的问题。"),
    "scenario": ("Scenario", "正则快速路径，低延迟处理常见场景。"),
    "pdor": ("PDOR", "Plan-Do-Observe-Reflect 循环，适合需要观察和反思的复杂任务。"),
    "agent_group": ("Agent Group", "多个智能体合作，适合复杂任务。"),
}

# 数据源标签兜底（数据源类没有元数据属性时使用）
_DS_LABELS_FALLBACK = {
    "mx_data": ("妙想", "东方财富 AI 接口，支持 A/HK/US 市场"),
    "sina": ("新浪财经", "新浪实时行情，仅 A 股"),
    "akshare": ("Akshare", "开源聚合接口，支持 A/HK/ETF"),
    "efinance": ("Efinance", "东方财富接口，支持 A/ETF"),
    "baostock": ("宝存金融", "历史数据为主，仅 A 股"),
    "tushare": ("Tushare", "专业数据接口，支持 A/HK"),
    "qq": ("腾讯财经", "腾讯实时行情，仅 A 股"),
    "yfinance": ("Yahoo", "雅虎财经，支持全球市场"),
    "finnhub": ("Finnhub", "美股数据接口"),
    "longbridge": ("长桥", "港股/美股实时行情"),
}

_modes_cache_lock = threading.Lock()
_modes_payload_cache: dict[str, Any] | None = None
_datasources_cache_lock = threading.Lock()
_datasources_payload_cache: tuple[float, dict[str, Any]] | None = None


def _build_modes_payload() -> dict[str, Any]:
    from agents import AgentFactory
    names = AgentFactory.list()
    items = []
    for name in names:
        # 只用 fallback 元数据，不调用 AgentFactory.get() 触发懒实例化
        # （首次 get() 会导入 agent 模块 → langgraph → ~15s 阻塞）
        fallback_label, fallback_desc = _MODE_LABELS_FALLBACK.get(name, (name, ""))
        items.append({"name": name, "label": fallback_label, "description": fallback_desc})
    default_mode = "react_stock" if "react_stock" in names else (names[0] if names else "react_stock")
    return {"current": default_mode, "modes": items}


def _get_modes_payload() -> dict[str, Any]:
    global _modes_payload_cache
    if _modes_payload_cache is not None:
        return _modes_payload_cache
    with _modes_cache_lock:
        if _modes_payload_cache is None:
            _modes_payload_cache = _build_modes_payload()
        return _modes_payload_cache


def _build_datasources_payload() -> dict[str, Any]:
    from tools.fetcher import _ensure_initialized
    _ensure_initialized()
    from tools.fetcher.base import DataSourceManager
    status = DataSourceManager.status(probe=False)
    # 构建 name → source 实例映射
    source_map = {s.name: s for s in DataSourceManager._sources}
    items = []
    for name, info in status.items():
        # 优先从数据源类读取 label/description，兜底用硬编码
        source = source_map.get(name)
        if source:
            label = getattr(source, "label", None) or _DS_LABELS_FALLBACK.get(name, (name, ""))[0]
            description = getattr(source, "description", None) or _DS_LABELS_FALLBACK.get(name, ("", ""))[1]
        else:
            label, description = _DS_LABELS_FALLBACK.get(name, (name, ""))
        items.append({
            "name": name,
            "label": label,
            "description": description,
            "available": info.get("available", False),
            "priority": info.get("priority", 0),
        })
    items.sort(key=lambda x: x["priority"], reverse=True)
    return {"sources": items}


def _get_datasources_payload(refresh: bool = False) -> dict[str, Any]:
    global _datasources_payload_cache
    now = monotonic()
    cached = _datasources_payload_cache
    if not refresh and cached and now - cached[0] < DATASOURCE_STATUS_CACHE_SECONDS:
        return cached[1]
    with _datasources_cache_lock:
        now = monotonic()
        cached = _datasources_payload_cache
        if not refresh and cached and now - cached[0] < DATASOURCE_STATUS_CACHE_SECONDS:
            return cached[1]
        payload = _build_datasources_payload()
        _datasources_payload_cache = (now, payload)
        return payload


@router.get("/api/modes")
async def modes():
    return _get_modes_payload()


@router.get("/api/datasources")
async def datasources(refresh: bool = False):
    return _get_datasources_payload(refresh=refresh)


@router.get("/api/agents")
async def agents(mode: str = None):
    """获取 agent 信息。mode=agent_group 时返回 sub-agents 列表。"""
    from agents import AgentFactory
    from agents.factory import AgentFactory as AF

    # 获取所有 modes 的基本信息
    modes_info = []
    for name in AF.list():
        agent = AF.get(name)
        modes_info.append({
            "name": name,
            "label": getattr(agent, "name", name),
            "description": getattr(agent, "description", ""),
        })

    # 如果指定了 mode=agent_group，返回 sub-agents
    sub_agents = []
    if mode == "agent_group":
        try:
            from agents.group.subagents import AGENT_CLASSES, get_all_agent_info
            for info in get_all_agent_info():
                sub_agents.append({
                    "name": info["agent_name"],
                    "label": info["display_name"],
                    "description": info["description"],
                    "skills": info.get("assigned_skills", []),
                })
        except Exception:
            pass

    return {"modes": modes_info, "sub_agents": sub_agents}


# ============================================================
# 系统管理
# ============================================================

import logging
from datetime import datetime as _datetime

logger = logging.getLogger(__name__)


def _write_restart_log(msg: str):
    """将重启日志写入 web 日志目录（与 WebRequestLogger 同级）"""
    try:
        from utils.app_paths import get_logs_dir
        log_dir = os.path.join(get_logs_dir(), "web")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{_datetime.now().strftime('%Y-%m-%d')}.log")
        timestamp = _datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} │ RESTART  │ {msg}\n")
    except Exception:
        pass  # 写日志失败不阻塞重启流程


@router.post("/api/system/restart")
async def restart_server():
    """重启 Web 服务

    两阶段重启，避免端口冲突：
    1. spawn 一个中间启动器（DETACHED + NO_WINDOW，不受父进程退出影响）
    2. 当前进程 flush 日志后 os._exit(0)，释放端口
    3. 启动器 sleep 2s 等待端口释放，然后 spawn 新服务进程

    注意：仅适用于单 worker 场景，多 worker 下仅重启收到请求的 worker。
    """
    import subprocess
    import tempfile

    def _restart():
        import time
        time.sleep(0.3)  # 等 HTTP 响应发完

        _write_restart_log("重启信号触发，准备退出当前进程")

        if sys.platform == 'win32':
            cwd = os.getcwd()
            # -m server + PYTHONPATH 确保 sys.path 正确，不依赖 CWD
            bat_path = os.path.join(tempfile.gettempdir(), '_sr_restart.bat')
            with open(bat_path, 'w', encoding='utf-8') as f:
                f.write(f'@echo off\n')
                f.write(f'cd /d "{cwd}"\n')
                f.write(f'set PYTHONPATH={cwd}\n')
                f.write(f'start "" /b "{sys.executable}" -m server\n')

            _write_restart_log(f"执行 {bat_path}")
            subprocess.Popen(
                ['cmd', '/c', bat_path],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _write_restart_log("当前进程执行 os._exit")
            os._exit(0)
        else:
            time.sleep(1)
            _write_restart_log("Unix 环境用 execv 替换当前进程")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, daemon=True).start()
    return {"status": "ok", "message": "服务正在重启，约 3 秒后恢复"}


@router.post("/api/system/shutdown")
async def shutdown_server():
    """关闭 Web 服务。

    立即终止当前进程。用于重启后找不到旧进程时手动清理，
    也适合开发调试时快速停止服务。

    注意：仅适用于单 worker 场景。
    """
    import time as _time

    def _shutdown():
        _time.sleep(0.3)  # 等 HTTP 响应发完
        _write_restart_log("关闭信号触发，进程即将退出")
        # flush 标准输出和日志
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"status": "ok", "message": "服务正在关闭"}
