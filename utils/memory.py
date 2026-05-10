"""
选股雷达 - 智能记忆管理
支持开关控制：新闻情感分析等不需要记忆的场景可关闭
超过阈值自动压缩
"""
from langchain_classic.memory import ConversationSummaryBufferMemory
from utils.llm_factory import get_llm
from config import Config
from utils.logger import RadarLogger


class MemoryManager:
    """
    智能记忆管理器
    - enabled=False 时不记录、不读取，节省 token
    - 超过 max_token_limit 自动用 LLM 压缩旧对话
    """

    def __init__(self, logger: RadarLogger):
        self.logger = logger
        self.enabled = Config.MEMORY_ENABLED
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
        if not self.enabled:
            return
        self._memory.save_context({"input": text}, {"output": ""})

    def add_ai(self, text: str):
        if not self.enabled:
            return
        self._memory.save_context({"input": ""}, {"output": text})

    def get_history(self) -> list:
        if not self.enabled:
            return []
        return self._memory.load_memory_variables({}).get('history', [])

    def clear(self):
        self._memory.clear()
        self.logger.info("M", "记忆已清空")