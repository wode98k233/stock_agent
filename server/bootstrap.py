"""Web 服务初始化。"""
import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from config import Config
from server.runtime import TaskRuntime
from server.state import WebAppState
from server.storage import WebStorage
from memory.sdk import MemorySDK, MemoryConfig, _build_embedding_fn, set_sdk

_logger = logging.getLogger(__name__)

# 启动期 embedding 冷启动最长等待秒数（Ollama 首次拉起 bge-m3 约 1~2 分钟）。
# 超过此值则降级为纯 FTS5，避免冷启动失败时永久阻塞对话。
EMBED_WARM_TIMEOUT = 180.0

# 记忆系统初始化单例守卫：防止启动期被重复调用（如 agent 被再次启用）导致双预热/双加载
_memory_init_lock = threading.Lock()
_memory_sdk_built = False
_memory_sdk_instance = None


def _apply_bounded_default_executor():
    """限制本 event loop 的默认线程池大小。

    asyncio 的 run_in_executor(None, ...) 默认用
    ThreadPoolExecutor(max_workers=min(32, os.cpu_count()+4))；
    在 20 核机上会开 24 个线程。server 里 warmup / kline 拉取 /
    记忆接口 / 通知都走这个默认池，导致 web 进程线程数虚高（实测 ~105）。
    这里把它限制为 Config.WEB_MAX_WORKERS（默认 8）。

    必须在任何 run_in_executor(None, ...) 调用之前执行，
    否则 asyncio 会先按默认大小创建线程池、之后无法回收。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _logger.debug("[bootstrap] 无 event loop，跳过默认线程池限制")
        return
    max_workers = Config.WEB_MAX_WORKERS
    if max_workers and max_workers > 0:
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="web-default",
            )
        )
        _logger.info("[bootstrap] event loop 默认线程池已限制为 %d 线程", max_workers)


def _warmup_langgraph_async():
    """后台预热 langgraph：用 loop.run_in_executor 避免阻塞 event loop。"""
    def _warm():
        try:
            from agents import AgentFactory
            _logger.info("[bootstrap] 后台预热 Agent 实例（首次导入 langgraph 约 15s）...")
            agent = AgentFactory.get()
            _logger.info("[bootstrap] Agent 预热完成: %s", agent.name)
        except Exception:
            _logger.debug("[bootstrap] Agent 预热跳过", exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _warm)
    except RuntimeError:
        _logger.debug("[bootstrap] 无 event loop，跳过 Agent 预热")


def _schedule_periodic_gc():
    """定期 GC：每 10 分钟触发一次，防止长运行进程内存堆积。

    后台 warmup 完成后会导入大量模块，且 langgraph 编译会产生临时对象。
    定期 GC 确保进程长时间运行后内存不会无限增长。
    """
    import gc

    async def _gc_loop():
        await asyncio.sleep(600)  # 首次延迟 10 分钟（等所有 warmup 完成）
        while True:
            try:
                collected = gc.collect()
                if collected > 0:
                    _logger.debug("[GC] 定期回收 %d 个对象", collected)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(600)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_gc_loop())
        _logger.debug("[GC] 定期 GC 已注册（间隔 10 分钟）")
    except RuntimeError:
        pass


def _schedule_episodic_cleanup(sdk, retention_days: int):
    """asyncio 定时任务：每 6 小时清理过期情景记忆。event loop 关闭时自动取消，无需手动管理。"""
    async def _cleanup_loop():
        from datetime import datetime, timedelta
        await asyncio.sleep(600)  # 首次延迟 10 分钟，避免和启动预热抢资源
        while True:
            try:
                before = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
                cleaned = sdk.clean_episodic(retention_days)
                if cleaned > 0:
                    _logger.info("[Memory] 定时清理: 删除 %d 条过期情景记忆 (早于 %s)", cleaned, before)
            except asyncio.CancelledError:
                break
            except Exception:
                _logger.warning("[Memory] 定时清理失败", exc_info=True)
            await asyncio.sleep(6 * 3600)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_cleanup_loop())
        _logger.info("[Memory] 情景记忆自动清理已注册 (保留 %d 天)", retention_days)
    except RuntimeError:
        _logger.warning("[Memory] 无 event loop，跳过情景记忆自动清理注册")


def _schedule_semantic_cleanup(sdk, interval_minutes: int):
    """asyncio 定时任务：按配置间隔清理过期语义记忆。"""
    if sdk is None or interval_minutes <= 0:
        _logger.info("[Memory] 自动清理已禁用 (interval=%d)", interval_minutes)
        return

    async def _cleanup_loop():
        await asyncio.sleep(900)  # 首次延迟 15 分钟
        while True:
            try:
                deleted = sdk.clean_expired()
                if deleted > 0:
                    _logger.info(
                        "[Memory] 语义记忆自动清理: 已删除 %d",
                        deleted,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                _logger.warning("[Memory] 语义记忆自动清理失败", exc_info=True)
            await asyncio.sleep(interval_minutes * 60)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_cleanup_loop())
        _logger.info("[Memory] 语义记忆自动清理已注册 (间隔 %d 分钟)", interval_minutes)
    except RuntimeError:
        _logger.warning("[Memory] 无 event loop，跳过语义记忆自动清理注册")


def bootstrap_web() -> WebAppState:
    """初始化 Web server 运行依赖。"""
    from utils.bootstrap_common import init_common
    from utils.logger import get_logger
    # 懒加载：WebAgentContext 会经 agent_runner → agents.factory → agents.base →
    # utils.memory 拉入 torch 全家桶（~7s）。放在函数内，避免 app 构造期就加载，
    # 让 torch 链推迟到 lifespan 启动阶段（已在 "Started server process" 之后）。
    from server.agent_runner import WebAgentContext

    result = init_common()

    # 限制 event loop 默认线程池（必须在任何 run_in_executor(None) 之前）
    _apply_bounded_default_executor()

    storage = WebStorage()
    storage.mark_interrupted_messages()
    # 清理重启前未完成的回测任务（避免 SSE 永远显示 running）
    try:
        from utils.cache.backtest_db import mark_interrupted_runs
        counts = mark_interrupted_runs()
        if any(counts.values()):
            _logger.info("[Backtest] 清理中断回测: %s", counts)
    except Exception as e:
        _logger.warning("[Backtest] 清理中断回测失败: %s", e)
    runtime = TaskRuntime()
    agent_context = WebAgentContext(
        skill_register=result.skill_register,
        session_stats=result.session_stats,
        _logger_factory=get_logger,
    )

    # 记忆系统初始化（轻量，后端在后台预热）
    memory_sdk = _init_memory_sdk()
    agent_context.memory_sdk = memory_sdk

    # Agent 预热已由 bootstrap_common.init_common() 在后台启动
    # （无需再次调用 _warmup_langgraph_async）

    # 定期 GC：预热完成后每 10 分钟触发一次，防止长运行进程内存堆积
    _schedule_periodic_gc()

    # 定时清理过期情景记忆
    _schedule_episodic_cleanup(memory_sdk, Config.MEMORY_EPISODIC_RETENTION_DAYS)
    _schedule_semantic_cleanup(memory_sdk, Config.MEMORY_AUTO_CLEANUP_INTERVAL_MINUTES)

    return WebAppState(
        storage=storage,
        runtime=runtime,
        agent_context=agent_context,
        memory_sdk=memory_sdk,
    )


def _init_memory_sdk():
    """初始化记忆系统 SDK — 启动关键路径只做轻量初始化，后台预热 embedding（冷启动）。

    守卫：启动期若被重复调用（如 agent 被再次启用 / 并发首次请求触发重 bootstrap），
    只初始化一次，避免双预热、双加载 embedding 模型。

    - 记忆禁用（MEMORY_ENABLED=false）：标记未启用且视为就绪，直接返回 None（不阻塞对话）
    - 记忆启用：标记 warming，后台预热 FTS5/ChromaDB/embedding/reranker，embedding 冷启动
      完成（或超时降级）后标记 ready
    """
    global _memory_sdk_built, _memory_sdk_instance
    with _memory_init_lock:
        if _memory_sdk_built:
            set_sdk(_memory_sdk_instance)
            return _memory_sdk_instance
        _memory_sdk_built = True
        try:
            from server.routes import meta as _meta
            if not Config.MEMORY_ENABLED:
                # 记忆系统禁用：标记未启用且视为就绪，不初始化 SDK，不阻塞对话
                _meta.set_memory_enabled(False)
                _memory_sdk_instance = None
                set_sdk(None)
                return None

            _meta.set_memory_enabled(True)
            _meta.set_memory_warming(True)

            from memory.sdk import MemorySDK, MemoryConfig, load_memory_config
            _mem_cfg = MemoryConfig(
                backend_mode=Config.STOCK_MEMORY_BACKEND,
                embedding_fn=_build_embedding_fn(),
                user_id="default",
                retrieval_top_k=Config.MEMORY_RETRIEVAL_TOP_K,
                max_prompt_tokens=Config.MEMORY_MAX_PROMPT_TOKENS,
                embedding_ready_timeout=2.0,
                embedding_ready_interval=0.5,
            )
            try:
                _persisted = load_memory_config()
                if "min_similarity" in _persisted:
                    _mem_cfg.min_similarity = float(_persisted["min_similarity"])
            except Exception:
                pass
            sdk = MemorySDK(_mem_cfg)

            # jieba 预热仍可同步（轻量 ~0.2s，有缓存时更快）
            from memory.backend import ensure_jieba_ready
            ensure_jieba_ready(_logger)

            _memory_sdk_instance = sdk
            set_sdk(sdk)
            # 后台预热：SQLite + ChromaDB + embedding 冷启动 + reranker 全部异步
            _warmup_memory_async(sdk)
            return sdk
        except Exception as e:
            _logger.debug("[Memory] 初始化跳过: %s", e)
            # 异常时降级为就绪，避免冷启动失败永久阻塞对话
            try:
                from server.routes import meta as _meta
                _meta.set_memory_ready()
            except Exception:
                pass
            _memory_sdk_instance = None
            set_sdk(None)
            return None


def _warmup_memory_async(sdk):
    """后台线程预热 Memory 后端（不阻塞启动）。

    包含：FTS5 SQLite 建表、ChromaDB PersistentClient 初始化、embedding 冷启动
    （触发 Ollama 加载 bge-m3，避免首请求冷加载阻塞）、reranker 模型加载。
    无论 embedding 是否最终就绪，结束都标记 memory_ready（超时/失败则降级纯 FTS5）。
    """
    def _warm():
        try:
            t0 = time.time()
            sdk.warmup()
            _logger.info("[Memory] 后台预热完成 (backend=%s, %.1fs)",
                         sdk.backend.name(), time.time() - t0)

            # embedding 冷启动：触发 Ollama 加载 bge-m3，避免首请求冷加载阻塞
            # fast_fail：Ollama 没开时立即降级，不空等 timeout 把对话堵死
            try:
                emb = getattr(sdk.backend, "_embedding", None)
                fn = getattr(sdk._config, "embedding_fn", None)
                if emb is not None and emb.is_available() and fn is not None:
                    from memory.backend.embedding import _wait_embedding_ready
                    ok = _wait_embedding_ready(
                        fn,
                        timeout=EMBED_WARM_TIMEOUT,
                        interval=Config.STOCK_MEMORY_EMBEDDING_READY_INTERVAL,
                        fast_fail_on_connection_error=True,
                    )
                    _logger.info("[Memory] embedding 冷启动 %s",
                                 "完成" if ok else "超时/失败（降级为 FTS5）")
            except Exception:
                _logger.debug("[Memory] embedding 冷启动探测跳过", exc_info=True)

            # Reranker 预热（独立 daemon 线程，不阻塞）
            # 注意：Config 已在模块级 line 10 导入为全局变量；此处不可再 `from config import Config`，
            # 否则会令整个 _warm 作用域的 Config 变成局部变量，导致 line 299 提前引用时抛 UnboundLocalError。
            try:
                _reranker = getattr(sdk.backend, "_reranker", None)
                if _reranker is not None and hasattr(_reranker, "warm"):
                    _preload = Config.STOCK_MEMORY_RERANKER_PRELOAD
                    if _preload:
                        _reranker.warm()
                        _logger.info("[Memory] reranker 启动预载已启用（模型常驻内存）")
                    else:
                        _logger.info("[Memory] reranker 按需加载（首次检索加载、空闲后自动卸载，省内存）")
            except Exception:
                pass
        except Exception:
            _logger.debug("[Memory] 后台预热跳过", exc_info=True)
        finally:
            # 无论 embedding 是否就绪，都标记记忆系统就绪，避免冷启动失败时永久阻塞对话
            try:
                from server.routes import meta as _meta
                _meta.set_memory_ready()
            except Exception:
                pass

    t = threading.Thread(target=_warm, name="memory-warmup", daemon=True)
    t.start()
    _logger.info("[Memory] SDK 已创建，后端后台预热中...")
