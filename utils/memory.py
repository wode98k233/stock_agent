"""
选股雷达 - 智能记忆管理
支持开关控制：新闻情感分析等不需要记忆的场景可关闭
超过阈值自动压缩
"""
import re

from config import Config
from utils.logger import RadarLogger
from langchain_core.messages import BaseMessage

# 只给前端渲染、对模型无意义的标记：
#   ① 图表 UUID 标记（report_builder 拼在报告尾部）
#   ② 仪表盘 JSON 块（dashboard_node 在 web 模式追加在报告末尾）
_HISTORY_MARKER_RES = (
    re.compile(r"@@DASHBOARD_START@@.*?@@DASHBOARD_END@@", re.S),
    re.compile(r"\s*@@CHART:[0-9a-zA-Z_-]+@@\s*"),
)


def strip_history_markers(text: str) -> str:
    """剥离图表/仪表盘标记，保留报告正文（用于写入记忆与进 prompt 的历史）。"""
    if not text or "@@" not in text:
        return text
    out = text
    for pattern in _HISTORY_MARKER_RES:
        out = pattern.sub(" ", out)
    return out.strip()


def _load_summary_memory_cls():
    """惰性加载 ``ConversationSummaryBufferMemory``（拿不到返回 None）。

    ⚠️ **必须在函数里导入，不能放模块级**（2026-10-16 实测）：
    ``langchain_classic.memory`` 的包 __init__ 会 eager 导入 ``combined`` / ``vectorstore``，
    进而拉进 ``sentence_transformers`` + ``transformers`` + ``torch``，
    **单次导入 16 秒**（`import langchain_classic.base_memory` 只要 1.05s，说明重的就是这个包）。

    而本模块被大量模块 import（多数只想拿 ``MemoryManager`` 这个名字），
    实测 475 个单测文件里**只有 1 个真正创建 MemoryManager** —— 模块级导入等于
    让每个进程白付 16 秒，直接拖垮 xdist 的并行度（更多 worker = 更多次白付）。

    改惰性后：不创建 MemoryManager 的进程只花一次函数调用；创建的那 1 个照常付费。
    """
    try:
        from langchain_classic.memory import ConversationSummaryBufferMemory
        return ConversationSummaryBufferMemory
    except ModuleNotFoundError:
        return None


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

        _summary_cls = _load_summary_memory_cls() if self.enabled else None
        if self.enabled and _summary_cls is None:
            self.enabled = False
            self.logger.warning("M", "langchain_classic 未安装，记忆功能已自动关闭")
        elif self.enabled:
            from utils.llm_factory import get_llm

            self._memory = _summary_cls(
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
