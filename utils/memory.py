"""
选股雷达 - 智能记忆管理
支持开关控制：新闻情感分析等不需要记忆的场景可关闭
超过阈值自动压缩
"""
from config import Config
from utils.logger import RadarLogger
from langchain_core.messages import BaseMessage

try:
    from langchain_classic.memory import ConversationSummaryBufferMemory
except ModuleNotFoundError:
    ConversationSummaryBufferMemory = None


def iter_history_messages(history):
    """将常见会话历史格式统一为 ``(user|assistant, content)``。"""
    for message in history or []:
        role = content = None
        if isinstance(message, BaseMessage):
            role = message.type
            content = message.content
        elif isinstance(message, dict):
            role = message.get("role") or message.get("type")
            content = message.get("content")
        elif isinstance(message, (list, tuple)) and len(message) == 2:
            role, content = message

        normalized_role = {
            "human": "user",
            "user": "user",
            "ai": "assistant",
            "assistant": "assistant",
        }.get(role)
        if normalized_role and isinstance(content, str) and content.strip():
            yield normalized_role, content.strip()


class MemoryManager:
    """
    智能记忆管理器
    - enabled=False 时不记录、不读取，节省 token
    - 超过 max_token_limit 自动用 LLM 压缩旧对话
    """

    def __init__(self, logger: RadarLogger):
        self.logger = logger
        self.enabled = Config.MEMORY_ENABLED
        self._memory = None

        if self.enabled and ConversationSummaryBufferMemory is None:
            self.enabled = False
            self.logger.warning("M", "langchain_classic 未安装，记忆功能已自动关闭")
        elif self.enabled:
            from utils.llm_factory import get_llm

            self._memory = ConversationSummaryBufferMemory(
                llm=get_llm(),
                max_token_limit=Config.MEMORY_MAX_TOKENS,
                return_messages=True,
                input_key="input",
                output_key="output",
            )
        self.logger.info("M", f"记忆管理器 | enabled={self.enabled} | max_tokens={Config.MEMORY_MAX_TOKENS}")

    # ── 开关 ──

    def disable(self):
        self.enabled = False

    def enable(self):
        self.enabled = True

    # ── 上下文管理器（临时关闭） ──

    class _TempDisable:
        def __init__(self, mgr):
            self.mgr = mgr
            self.was_enabled = mgr.enabled

        def __enter__(self):
            self.mgr.disable()
            return self.mgr

        def __exit__(self, *args):
            if self.was_enabled:
                self.mgr.enable()

    def temp_disable(self):
        """with memory.temp_disable(): ...  临时关闭记忆"""
        return self._TempDisable(self)

    # ── 读写 ──

    def add_user(self, text: str):
        if not self.enabled or self._memory is None:
            return
        self._memory.save_context({"input": text}, {"output": ""})

    def add_ai(self, text: str):
        if not self.enabled or self._memory is None:
            return
        self._memory.save_context({"input": ""}, {"output": text})

    def append_turn(self, user_text: str, assistant_text: str):
        """原子写入一轮完整对话，避免产生空白 Human/AI 消息。"""
        if not self.enabled or self._memory is None:
            return
        self._memory.save_context(
            {"input": user_text},
            {"output": assistant_text},
        )

    def get_history(self) -> list:
        if not self.enabled or self._memory is None:
            return []
        history = self._memory.load_memory_variables({}).get('history', [])
        # 过滤空 content 消息：add_user/add_ai 分开调用时 save_context 会写入
        # 空对端消息（如 add_user 产生 human=text + ai=""），污染上下文并浪费 token
        return [m for m in history if getattr(m, "content", "").strip()]

    def clear(self):
        if self._memory is None:
            return
        self._memory.clear()
        self.logger.info("M", "记忆已清空")
