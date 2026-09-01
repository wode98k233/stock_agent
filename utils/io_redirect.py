"""
IO 重定向工具
把 stdout 和 stderr 重定向到 logger，同时保留控制台输出
"""
import sys
import logging

_ERROR_KEYWORDS = ("失败", "错误", "Error", "Warning", "Exception", "Traceback")


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
        if self.original_stream:
            try:
                self.original_stream.write(data)
                self.original_stream.flush()
            except:
                pass

        if data.strip():
            try:
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
