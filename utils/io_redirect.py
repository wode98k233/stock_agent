"""
IO 重定向工具
把 stdout 和 stderr 重定向到 logger，同时保留控制台输出
"""
import sys
import logging
import re
import threading

_ERROR_KEYWORDS = ("失败", "错误", "Error", "Warning", "Exception", "Traceback")

# 重入哨兵（线程局部）。见 LoggerStream.write 内的说明。
_write_state = threading.local()

# 进度条（tqdm 等）识别：写到 stderr 时不应被当成 ERROR 噪声。
# 典型形态：` 33%|####| 1/3 [00:00<00:01, 1.74it/s]`；tqdm 重绘用 \r。
_PROGRESS_RE = re.compile(r"(\d+%\s*\|)|(\|\s*\d+/\d+\s*\[)|(it/s)|(s/it)|(\[\d+:\d+<)")

# Python warnings 块识别：warnings 模块把「位置行 + 源码行」一次 write 到 stderr，
# 形态如 `D:\...\cerebro.py:1543: DeprecationWarning: ...\n  qstart = ...`。
# 第三方库告警在 warnings 过滤器被改宽时会高频重复（实测 backtrader 的 utcnow
# 弃用告警单次批量回测刷了 1027 行），全部按 ERROR 记录会把日志刷爆。
# 政策：同一条警告只记首条（WARNING），重复按次抑制，每 100 次补一条计数。
_WARNING_HEAD_RE = re.compile(r"^.+?:\d+: [A-Za-z_]*Warning: ")
_warn_seen: dict = {}
_warn_lock = threading.Lock()
_WARN_REPORT_EVERY = 100


def _is_progress_bar(data) -> bool:
    """判断是否为进度条输出（含 \\r 重绘或 tqdm 形态）。非字符串（如 bytes）一律视为否。"""
    if not isinstance(data, str):
        return False
    if "\r" in data:
        return True
    return bool(_PROGRESS_RE.search(data))


def _is_python_warning(data) -> bool:
    """判断是否为 Python warnings 模块输出的告警块（Deprecation/Resource/Future/User 等）。"""
    return isinstance(data, str) and bool(_WARNING_HEAD_RE.match(data))


class LoggerStream:
    def __init__(self, logger, level, original_stream):
        self.logger = logger
        self.level = level
        self.original_stream = original_stream
        self._is_radar = hasattr(logger, 'info') and hasattr(logger, '_logger')

    def _should_log_as_error(self, data: str) -> bool:
        """stdout 中包含错误关键词时提升为 WARNING"""
        return any(kw in data for kw in _ERROR_KEYWORDS)

    def write(self, data):
        # ── 重入保护 ────────────────────────────────────────────────────
        # logging 的 StreamHandler.emit 写失败后会调 handleError，而 handleError
        # 直接写 sys.stderr —— 此时 sys.stderr 就是本对象。于是：
        #   emit 失败 → handleError → 本 write → logger.error → emit 失败 → …
        # 无限递归，且每轮都把 traceback 灌进日志（单次可刷数万行、CPU 打满）。
        # write 内的 try/except 拦不住，因为递归发生在 logger.error() 内部，
        # 异常根本不会抛回来。
        # 处置：同一线程已在 write 内时，只落原始流、不再进 logger。
        if getattr(_write_state, "in_write", False):
            if self.original_stream:
                try:
                    self.original_stream.write(data)
                    self.original_stream.flush()
                except Exception:
                    pass
            return

        _write_state.in_write = True
        try:
            self._write_inner(data)
        finally:
            _write_state.in_write = False

    def _log_python_warning(self, data: str) -> None:
        """Python 告警块去重：同一条只记首条（WARNING），每 100 次补一条计数（DEBUG）。"""
        head = data.split("\n", 1)[0].strip()
        with _warn_lock:
            n = _warn_seen.get(head, 0) + 1
            _warn_seen[head] = n
        if n != 1 and n % _WARN_REPORT_EVERY != 0:
            return
        text = head if n == 1 else f"（同类警告已重复 {n} 次，继续抑制）{head}"
        level = logging.WARNING if n == 1 else logging.DEBUG
        try:
            if self._is_radar:
                if level >= logging.WARNING:
                    self.logger.warning("IO", text)
                else:
                    self.logger.debug("IO", text)
            else:
                self.logger.log(level, text)
        except Exception:
            pass

    def _write_inner(self, data):
        if self.original_stream:
            try:
                self.original_stream.write(data)
                self.original_stream.flush()
            except:
                pass

        if data.strip():
            try:
                # 进度条（tqdm 等）只保留控制台输出，不进日志（否则刷屏 ERROR）
                if _is_progress_bar(data):
                    return
                # Python 告警块（第三方库 warnings）：去重后记录，不再按 ERROR 刷屏
                if _is_python_warning(data):
                    self._log_python_warning(data)
                    return
                if self._is_radar:
                    tag = "IO"
                    if self.level >= logging.ERROR:
                        self.logger.error(tag, data.rstrip('\n'))
                    elif self._should_log_as_error(data):
                        self.logger.warning(tag, data.rstrip('\n'))
                    elif self.level >= logging.WARNING:
                        self.logger.warning(tag, data.rstrip('\n'))
                    else:
                        # stdout 默认降为 DEBUG，减少第三方库噪音
                        self.logger.debug(tag, data.rstrip('\n'))
                else:
                    effective_level = self.level
                    if self.level < logging.WARNING and self._should_log_as_error(data):
                        effective_level = logging.WARNING
                    self.logger.log(effective_level, data.rstrip('\n'))
            except:
                pass

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except:
                pass


def redirect_stdio_to_logger(logger):
    """
    把 stdout 和 stderr 重定向到 logger

    返回: (old_stdout, old_stderr) 用于恢复
    """
    old_stdout = sys.stdout
    old_stderr = sys.stderr

    sys.stdout = LoggerStream(logger, logging.DEBUG, old_stdout)
    sys.stderr = LoggerStream(logger, logging.ERROR, old_stderr)

    return old_stdout, old_stderr


def restore_stdio(old_stdout, old_stderr):
    """恢复 stdout 和 stderr"""
    sys.stdout = old_stdout
    sys.stderr = old_stderr
