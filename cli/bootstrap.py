"""
CLI 初始化引导
负责环境、目录、日志级别的初始化
"""
import asyncio

from memory.sdk import get_sdk


async def bootstrap():
    """
    初始化引导
    流程：共享初始化 → memory → banner
    """
    from utils.bootstrap_common import init_common
    result = init_common()
    skill_register = result.skill_register
    session_stats = result.session_stats

    from utils.logger import get_logger
    from config import Config

    from utils.memory import MemoryManager
    logger, _, _, _ = get_logger("system", skip_db=True)
    memory = MemoryManager(logger)

    # 记忆系统初始化
    _init_memory_sdk(logger)

    # 统计 Skill 和模板数量
    skill_count = len(skill_register.get_all_skills())
    template_count = 0
    try:
        from agents.analysis.template_store import _load_index
        template_count = len(_load_index())
    except Exception:
        pass

    from cli.banner import build_banner
    banner = build_banner(
        mode="react_stock",
        model=Config.OPENAI_MODEL_NAME,
        trace="on" if Config.ENABLE_TRACE else "off",
        skill_count=skill_count,
        template_count=template_count,
        cache="enabled" if Config.CACHE_PREFIX_ENABLED else "disabled",
    )

    return {"banner": banner, "skill_register": skill_register, "session_stats": session_stats, "memory": memory}


def _init_memory_sdk(logger):
    """初始化记忆系统 SDK（失败不影响主流程）

    启动关键路径不做 warmup，FTS5/SQLite 在首次检索时懒初始化。
    ChromaDB/embedding 由后台线程异步预热。
    """
    try:
        sdk = get_sdk()
        from memory.backend import ensure_jieba_ready
        ensure_jieba_ready(logger)
        # 后台预热（不阻塞 CLI 交互）
        import threading
        def _warm():
            try:
                sdk.warmup()
                logger.info("[Memory] 后台预热完成 backend=%s", sdk.backend.name())
            except Exception:
                logger.debug("[Memory] 后台预热跳过", exc_info=True)
        threading.Thread(target=_warm, name="memory-warmup", daemon=True).start()
        logger.info("[Memory] SDK 已创建，后端后台预热中...")
    except Exception as e:
        logger.debug("[Memory] 记忆系统初始化跳过: %s", e)
