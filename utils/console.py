"""控制台兼容处理。

打包后的 Windows 控制台不一定支持 emoji 和制表符号。这里把编码初始化和
可选的纯文本降级集中到入口层，避免业务代码到处判断运行环境。
"""
import os
import re
import sys


_ICON_REPLACEMENTS = {
    "✅": "[OK]",
    "❌": "[ERROR]",
    "⚠️": "[WARN]",
    "⚠": "[WARN]",
    "ℹ️": "[INFO]",
    "ℹ": "[INFO]",
    "📊": "[STATS]",
    "📖": "[HELP]",
    "📦": "[AGENT]",
    "📡": "[RADAR]",
    "💡": "[TIP]",
    "📝": "[LOG]",
    "🔍": "[TRACE]",
    "🔧": "[DEBUG]",
    "🔄": "[MODE]",
    "🚀": "[RUN]",
    "👋": "[BYE]",
    "🌳": "[TREE]",
}

_BOX_REPLACEMENTS = {
    "╔": "+",
    "╗": "+",
    "╚": "+",
    "╝": "+",
    "║": "|",
    "═": "=",
    "─": "-",
    "│": "|",
    "┌": "+",
    "┐": "+",
    "└": "+",
    "┘": "+",
    "├": "+",
    "┤": "+",
    "┬": "+",
    "┴": "+",
    "┼": "+",
    "╭": "+",
    "╮": "+",
    "╰": "+",
    "╯": "+",
    "→": "->",
    "←": "<-",
    "↔": "<->",
    "•": "-",
    "●": "*",
    "◆": "*",
}

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "]"
)

_HORIZONTAL_BOX_CHARS = set("─━═")
_VERTICAL_BOX_CHARS = set("│┃║")


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def is_plain_console_enabled() -> bool:
    """是否启用纯文本控制台输出。"""
    value = os.getenv("STOCK_RADAR_PLAIN_CONSOLE")
    if value is not None:
        return _truthy(value) and not _falsey(value)
    return bool(getattr(sys, "frozen", False))


def sanitize_console_text(text: str) -> str:
    """把控制台不稳定字符降级为 ASCII，保留中文内容。"""
    if not is_plain_console_enabled():
        return text

    for old, new in _ICON_REPLACEMENTS.items():
        text = text.replace(old, new)
    for old, new in _BOX_REPLACEMENTS.items():
        text = text.replace(old, new)
    text = _EMOJI_PATTERN.sub("", text)
    text = text.replace("\ufe0f", "")
    text = "".join(_fallback_box_char(ch) for ch in text)
    return text


def _fallback_box_char(ch: str) -> str:
    code = ord(ch)
    if 0x2500 <= code <= 0x257F:
        if ch in _HORIZONTAL_BOX_CHARS:
            return "-"
        if ch in _VERTICAL_BOX_CHARS:
            return "|"
        return "+"
    return ch


class SafeConsoleStream:
    """写入控制台前做字符降级的轻量包装。"""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, data):
        return self._wrapped.write(sanitize_console_text(data))

    def flush(self):
        return self._wrapped.flush()

    def isatty(self):
        return self._wrapped.isatty()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _configure_windows_code_page():
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _enable_windows_vt100() -> bool:
    """尝试启用 Windows 终端 VT100 处理（支持 ANSI 转义码）。返回是否成功。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
        if handle == -1 or handle is None:
            return False

        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False

        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if kernel32.SetConsoleMode(handle, new_mode):
            os.environ["_VT100_ENABLED"] = "1"
            return True
    except Exception:
        pass
    return False


def _reconfigure_stream(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def configure_console():
    """初始化 Windows 控制台编码，并在打包态默认启用纯文本输出。"""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    _configure_windows_code_page()
    _enable_windows_vt100()
    _reconfigure_stream(sys.stdout)
    _reconfigure_stream(sys.stderr)

    if is_plain_console_enabled():
        if not isinstance(sys.stdout, SafeConsoleStream):
            sys.stdout = SafeConsoleStream(sys.stdout)
        if not isinstance(sys.stderr, SafeConsoleStream):
            sys.stderr = SafeConsoleStream(sys.stderr)
