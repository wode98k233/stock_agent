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


# ── 启动导入门闸（2026-10-01 web 冻结修复）─────────────────────────
# 机制：预热线程（agent/skill/memory）import 期间持有各包的 import 模块锁
# （如 `from agents import AgentFactory` 会锁住 agents 包直到整棵依赖树导入完）。
# 请求 handler 若在此窗口执行函数体懒加载（如 route_preview 的
# `from agents.analysis.template_store import ...`），事件循环线程会阻塞在同一把
# import 锁上 → 整站冻结（实例：2026-10-01 12:24 启动，route-preview 143993ms、
# snapshot 151729ms，health 停摆 2m24s）。
# 门闸语义：预热线程启动时 armed；两个导入阶段都完成（或失败，finally 兜底）后
# 放行。未 armed（CLI / 测试 / 未起预热）默认放行。请求侧在 server/middleware.py
# 的 WarmupGateMiddleware 异步等待，事件循环不阻塞。
_import_gate_lock = threading.Lock()
_import_gate_armed = False
_agent_imports_done = False
_skill_imports_done = False


def _arm_import_gate():
    global _import_gate_armed
    with _import_gate_lock:
        _import_gate_armed = True


def _mark_agent_imports_done():
    global _agent_imports_done
    with _import_gate_lock:
        _agent_imports_done = True


def _mark_skill_imports_done():
    global _skill_imports_done
    with _import_gate_lock:
        _skill_imports_done = True


def imports_ready() -> bool:
    """启动导入门闸：False = 预热线程仍在 import，请求侧不得执行重模块懒加载。"""
    with _import_gate_lock:
        if not _import_gate_armed:
            return True
        return _agent_imports_done and _skill_imports_done


def _reset_import_gate_for_tests():
    """仅测试用：恢复门闸初始状态（未 armed = 放行）。"""
    global _import_gate_armed, _agent_imports_done, _skill_imports_done
    with _import_gate_lock:
        _import_gate_armed = False
        _agent_imports_done = False
        _skill_imports_done = False


def _bootstrap_logger():
    """惰性获取启动期 logger：skip_db=True（无独立 dialog 文件），记录经共享根 handler 兜底落盘。"""
    global _BOOTSTRAP_LOGGER
    if _BOOTSTRAP_LOGGER is None:
        from utils.logger import get_logger
        _BOOTSTRAP_LOGGER = get_logger("system-bootstrap", skip_db=True)[0]
    return _BOOTSTRAP_LOGGER


def init_common(start_warmups: bool = True) -> BootstrapResult:
    """执行 CLI/Server 共享的初始化步骤。每步打印耗时，方便排查启动瓶颈。

    start_warmups=False：跳过 Skill / Agent 后台预热（Web 场景由 lifespan 在
    路由注册完成后自行启动，避免预热线程与路由导入在 GIL 下争抢导致启动翻倍变慢）。
    """    

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

    # 5b. Dashboard llm_fields 白名单校验（pydantic 硬白名单会静默丢弃未知字段，
    #     新增 dashboard 字段漏改 schema 时在此显式检出；纯 JSON/内存操作，<10ms）
    from agents.analysis.dashboard_config import validate_dashboards_llm_fields
    problems = validate_dashboards_llm_fields()
    if problems:
        _bootstrap_logger().warning("U", f"[bootstrap] Dashboard llm_fields 校验发现 {len(problems)} 个问题（详见上方 WARNING）")

    # 6. 后台加载 Skill 模块（不阻塞启动；Web 场景由 lifespan 在路由后启动）
    if start_warmups:
        _start_skill_warmup(skill_register)

    # 7. 后台预热 Agent（首次 import langgraph + compile graph，~15s）
    #    对 CLI 和 Web 都有效，避免首请求阻塞；Web 场景在路由注册后启动
    if start_warmups:
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
            _mark_skill_imports_done()  # 失败也放行，绝不让门闸永久关闭
            _maybe_gc("[skill-warmup]")

    t = threading.Thread(target=_warm, name="skill-warmup", daemon=True)
    _arm_import_gate()
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
            try:
                agent = AgentFactory.get()
            finally:
                # import 阶段结束即放行门闸（graph 预编译是纯 CPU，不再持有 import 锁）
                _mark_agent_imports_done()
            logger.info("U", f"[bootstrap] Agent 导入完成: {agent.name}，开始预编译 graph...")
            agent.warmup_graph(logger=logger)
            logger.info("U", f"[bootstrap] Agent 预热完成: {agent.name}")
        except Exception:
            _bootstrap_logger().warning("U", "[bootstrap] Agent 预热跳过")
        finally:
            _mark_agent_imports_done()  # 幂等兜底：get() 抛异常时也必须放行
            _maybe_gc("[agent-warmup]")
            _notify_readiness_full()

    t = threading.Thread(target=_warm, name="agent-warmup", daemon=True)
    _arm_import_gate()
    t.start()


def start_skill_warmup(skill_register):
    """公开入口：后台加载 Skill 模块（供 Web lifespan 在路由注册完成后调用）。"""
    _start_skill_warmup(skill_register)


def start_agent_warmup():
    """公开入口：后台预热 Agent（供 Web lifespan 在路由注册完成后调用，避免 GIL 争抢）。"""
    _start_agent_warmup()


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
