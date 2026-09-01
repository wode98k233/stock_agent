"""CLI/Server 共享的初始化步骤

消除 cli/bootstrap.py 和 server/bootstrap.py 的重复初始化逻辑。

启动优化：Skill 发现分两阶段 — 关键路径只解析 SKILL.md 元数据，
Python 模块在后台线程加载，避免 import 重型库拖慢启动。
"""
from dataclasses import dataclass
from typing import Any
import gc
import time
import threading

@dataclass
class BootstrapResult:
    """共享初始化结果"""
    skill_register: Any
    session_stats: Any


_BOOTSTRAP_LOGGER = None


def _bootstrap_logger():
    """惰性获取启动期 logger：skip_db=True（无独立 dialog 文件），记录经共享根 handler 兜底落盘。"""
    global _BOOTSTRAP_LOGGER
    if _BOOTSTRAP_LOGGER is None:
        from utils.logger import get_logger
        _BOOTSTRAP_LOGGER = get_logger("system-bootstrap", skip_db=True)[0]
    return _BOOTSTRAP_LOGGER


def init_common() -> BootstrapResult:
    """执行 CLI/Server 共享的初始化步骤。每步打印耗时，方便排查启动瓶颈。"""    

    def _step(name):
        _bootstrap_logger().info("U", f"[bootstrap] {name} ...")
        return time.time()

    def _done(name, t0):
        _bootstrap_logger().info("U", f"[bootstrap] {name} done ({time.time() - t0:.1f}s)")

    # 1. 环境
    t0 = _step("generate_env")
    from utils.env_helper import generate_env_from_template
    generate_env_from_template()
    _done("generate_env", t0)

    # 2. 目录
    t0 = _step("ensure_app_dirs")
    from utils.app_paths import ensure_app_dirs
    ensure_app_dirs()
    _done("ensure_app_dirs", t0)

    # 3. 日志级别 + 共享根 handler（首次 import utils.logger 会触发 utils.cache，不再阻塞建表）
    t0 = _step("log_level")
    from config import Config
    from utils.logger import set_global_log_level, install_root_handler
    install_root_handler()
    set_global_log_level(Config.LOG_LEVEL)
    _done("log_level", t0)

    # 3b. 初始化缓存数据库表（原在 utils.cache 模块导入时执行，现延迟至此）
    t0 = _step("init_cache_tables")
    from utils.cache.core import ensure_cache_tables
    ensure_cache_tables()
    _done("init_cache_tables", t0)

    # 4. SkillRegister — 快速模式：只解析 SKILL.md 元数据，不 import 模块
    t0 = _step("SkillRegister.auto_discover")
    from tools.skill_register import SkillRegister
    skill_register = SkillRegister()
    skill_register.auto_discover(fast=True)
    _done("SkillRegister.auto_discover", t0)

    # 5. 注册 Agent
    t0 = _step("register_all_agents")
    from agents import register_all
    register_all()
    _done("register_all_agents", t0)

    # 6. 后台加载 Skill 模块（不阻塞启动）
    _start_skill_warmup(skill_register)

    # 7. 后台预热 Agent（首次 import langgraph + compile graph，~15s）
    #    对 CLI 和 Web 都有效，避免首请求阻塞
    _start_agent_warmup()

    # 8. SessionStats
    t0 = _step("SessionStats")
    from utils.session_stats import SessionStats
    session_stats = SessionStats()
    _done("SessionStats", t0)

    # 通知 Web 层：T1 基础服务就绪
    _notify_readiness_basic()

    # 启动关键路径完成，强制 GC 回收初始化过程中的临时对象
    _maybe_gc("[bootstrap]")

    return BootstrapResult(skill_register=skill_register, session_stats=session_stats)


def _start_skill_warmup(skill_register):
    """后台线程加载 Skill Python 模块，不阻塞启动。完成后强制 GC 回收临时内存。"""
    def _warm():
        try:
            skill_register.warmup_modules()
        except Exception:
            _bootstrap_logger().debug("U", "[bootstrap] Skill 模块预热跳过", exc_info=True)
        finally:
            _maybe_gc("[skill-warmup]")

    t = threading.Thread(target=_warm, name="skill-warmup", daemon=True)
    t.start()


def _start_agent_warmup():
    """后台线程预热 Agent：import + graph 预编译（~15-20s）。

    CLI 和 Web 共享此逻辑，避免首个分析请求阻塞。
    预热完成后通知 Web 层 T2 就绪，并强制 GC 回收 graph 编译产生的临时内存。
    """
    def _warm():
        try:
            from agents import AgentFactory
            from utils.logger import get_logger
            logger, _, _, _ = get_logger("system", skip_db=True)
            logger.info("U", "[bootstrap] 后台预热 Agent（首次 import langgraph）...")
            agent = AgentFactory.get()
            logger.info("U", f"[bootstrap] Agent 导入完成: {agent.name}，开始预编译 graph...")
            agent.warmup_graph(logger=logger)
            logger.info("U", f"[bootstrap] Agent 预热完成: {agent.name}")
        except Exception:
            _bootstrap_logger().warning("U", "[bootstrap] Agent 预热跳过")
        finally:
            _maybe_gc("[agent-warmup]")
            _notify_readiness_full()

    t = threading.Thread(target=_warm, name="agent-warmup", daemon=True)
    t.start()


def _maybe_gc(label: str = ""):
    """强制 GC 并打印回收统计（用于后台预热完成后释放临时内存）。"""
    try:
        collected = gc.collect()
        if collected > 0:
            _bootstrap_logger().info("U", f"[bootstrap] {label} GC 回收 {collected} 个对象")
    except Exception:
        pass


def _notify_readiness_basic():
    """通知 Web 层 T1 基础服务已就绪。"""
    try:
        from server.routes.meta import set_readiness_basic
        set_readiness_basic()
    except Exception:
        pass  # CLI 模式没有 server context，忽略


def _notify_readiness_full():
    """通知 Web 层 T2 全部服务已就绪（agent 预热完成）。"""
    try:
        from server.routes.meta import set_readiness_full
        set_readiness_full()
    except Exception:
        pass  # CLI 模式没有 server context，忽略
