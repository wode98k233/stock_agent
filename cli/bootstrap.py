"""
CLI 初始化引导
负责环境、目录、日志级别的初始化
"""
import asyncio


async def bootstrap():
    """
    初始化引导
    📡 初始化流程：环境 → 目录 → 日志级别 → skill_register → memory
    """
    from utils.env_helper import ensure_env_file
    ensure_env_file()

    from utils.app_paths import ensure_app_dirs
    ensure_app_dirs()

    from utils.logger import set_global_log_level, get_logger
    from config import Config
    set_global_log_level(Config.LOG_LEVEL)

    from tools.skill_register import SkillRegister
    skill_register = SkillRegister()
    skill_register.auto_discover()

    from utils.memory import MemoryManager
    logger, _, _ = get_logger("system", skip_db=True)
    memory = MemoryManager(logger)

    from cli.banner import build_banner
    banner = build_banner()

    from utils.session_stats import SessionStats
    session_stats = SessionStats()

    return {"banner": banner, "skill_register": skill_register, "session_stats": session_stats, "memory": memory}