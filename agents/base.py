"""
🤖 Agent 基类
所有 Agent 模式需继承此基类
"""
import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime

from utils.logger import ensure_radar
from utils.memory import iter_history_messages
from memory.sdk import get_sdk
from memory.user_profile import UserProfileManager


def _build_memory_context(user_input: str, history: list = None) -> Optional[str]:
    """从 session 历史提取最近的用户提问，拼接成记忆检索上下文。

    仅取历史中的 human/user 消息（排除当前这一条，避免与 query 重复），
    最多取最近 4 条。无历史时返回 None（退化成仅用当前 query 检索）。
    """
    if not history:
        return None
    human_texts = []
    cur = (user_input or "").strip()
    for role, text in iter_history_messages(history):
        if role == "user" and text != cur:
            human_texts.append(text)
    if not human_texts:
        return None
    return "\n".join(human_texts[-4:])


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

    def warmup_graph(self, logger=None):
        """预热 Graph 编译（后台线程调用）。

        子类可覆写以预编译 Graph，让首次请求时 compile 几乎瞬间完成。
        基类默认不做任何事。
        """
        pass

    def _update_intent_memory(self, user_input: str, final_response: str, logger):
        """更新用户画像（非阻塞）。

        实体提取策略:
          1. 从 user_input 提取板块/个股 (短文本, 准确)
          2. 从 ContextVar 工具提取补充股票代码 (工具返回的具体股票)
          3. 不再正则解析完整报告文本 → 避免垃圾stock名
        """

        logger = ensure_radar(logger)
        try:
            profile = UserProfileManager(user_id="default")
            result_str = final_response if isinstance(final_response, str) else str(final_response or "")

            # 1. 从用户输入的短文本提取板块/个股 (比正则解析报告全文准确得多)
            sectors, stocks = profile.extract_entities(user_input)

            # 2. 从 ContextVar 工具提取中补充股票代码和名称
            try:
                from memory.metadata import _tool_extracts, _INIT_SENTINEL
                extracts = _tool_extracts.get()
                if extracts is not _INIT_SENTINEL:
                    for ex in extracts:
                        code = ex.get("stock_code")
                        name = ex.get("stock_name")
                        if code and code not in stocks:
                            stocks.append(code)
                        if name and name not in stocks and len(name) >= 2:
                            stocks.append(name)
            except Exception as e:
                logger.error(f"从 ContextVar 工具提取中补充股票代码和名称失败: {e}")

            # 3. 从对话隐式提取风险偏好/投资周期等
            profile.extract_from_conversation(user_input)

            profile.update(
                query=user_input,
                result=result_str,
                sectors=sectors,
                stocks=stocks,
            )
        except Exception as e:
            logger.error("I", f"[UserProfile] 更新失败（非阻塞）: {e}")

    def _enrich_user_input(self, user_input: str, progress_callback=None,
                           history: list = None) -> str:
        """构建增强输入。enrich_with_time 已生成 [背景信息]+[用户问题] 骨架，
        我们只追加画像和记忆，不重复加 [用户问题] 头。

        同时把「用户原始输入 / 用户画像 / 用户相关记忆片段」三块通过
        progress_callback 推给前端，让页面也能展示（不再只是 agent 内部）。

        Args:
            history: session 历史消息（LangChain 消息元组列表），用于拼接记忆检索上下文。
        """
        from output.time_util import enrich_with_time
        enriched = enrich_with_time(user_input)
        profile_ctx = ""
        memory_block = ""

        # 市场开市/休市状态
        try:
            from utils.trading_calendar import get_market_status_text
            market_status = get_market_status_text()
            if market_status and "正常开市" not in market_status:
                enriched += f"\n[市场状态] {market_status}"
        except Exception:
            pass

        # 用户画像
        try:
            profile = UserProfileManager(user_id="default")
            profile_ctx = profile.get_context_hint() or ""
            if profile_ctx:
                enriched += "\n" + profile_ctx
        except Exception:
            profile_ctx = ""

        # 相关记忆片段（方案A：拼接 session 历史提升召回）
        try:
            sdk = get_sdk()
            context = _build_memory_context(user_input, history)
            memory_block = sdk.retrieve(user_input, context=context) or ""
            if memory_block:
                enriched += "\n" + memory_block
        except Exception:
            memory_block = ""

        # 推送「记忆上下文」进度事件：用户原始输入 / 用户画像 / 用户相关记忆片段
        # 始终推送：即便画像/记忆为空，用户也能在页面看到「用户原始输入」这一块，
        # 避免空记忆场景下页面什么都不显示（与用户预期「页面应能展示记忆上下文」一致）。
        if progress_callback:
            try:
                from utils.progress import ProgressEvent, ProgressType
                progress_callback(ProgressEvent(
                    type=ProgressType.MEMORY_CONTEXT,
                    message="🧠 记忆上下文已构建",
                    data={
                        "raw_input": user_input,
                        "profile": profile_ctx,
                        "memory_fragments": memory_block,
                    },
                ))
            except Exception:
                pass

        return enriched
