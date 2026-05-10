"""
🤖 Agent 基类
所有 Agent 模式需继承此基类
"""
import asyncio
from typing import Optional, Callable
from datetime import datetime


class BaseAgent:
    """
    🤖 Agent 基类
    📋 定义 Agent 的标准接口
    """

    name: str = ""
    description: str = ""

    async def run(self, user_input: str, registry, memory, logger, progress_callback: Optional[Callable] = None):
        """
        🚀 执行 Agent
        📥 user_input: 用户输入
        📥 registry: Skill 注册器
        📥 memory: 记忆管理器
        📥 logger: 日志器
        📥 progress_callback: 进度回调函数
        📤 str: Agent 执行结果
        """
        raise NotImplementedError

    def on_startup(self):
        """
        🆕 Agent 启动时的初始化：清理过期缓存
        """
        from utils.cache import async_clean_expired_cache
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_clean_expired_cache())
        except RuntimeError:
            pass

    def _update_intent_memory(self, user_input: str, final_response: str, logger):
        """更新意图记忆（非阻塞）"""
        from utils.logger import ensure_radar

        logger = ensure_radar(logger)
        try:
            from utils.intent import IntentMemoryManager
            intent_mgr = IntentMemoryManager(user_id="default")
            sectors, stocks = intent_mgr.extract_entities(final_response or "")
            intent_mgr.update(
                query=user_input,
                result=final_response or "",
                sectors=sectors,
                stocks=stocks,
            )
        except Exception as e:
            logger.debug("I", f"[IntentMemory] 更新失败（非阻塞）: {e}")

    def _enrich_user_input(self, user_input: str) -> str:
        from output.time_util import enrich_with_time
        enriched = enrich_with_time(user_input)
        return enriched + "\n\n[额外要求]\n如果有mx_相关工具,优先使用"
