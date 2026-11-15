"""utils/io_redirect 重入死循环回归测试

**事故背景（2026-10-13）**
跑 ReAct e2e 时日志文件 ``2026-10-13-18-14-6ef97bb8-*.log`` 出现 51380 行，其中
34858 行是同一段 logging 调用栈在原地打转，进程 CPU 打满 1204 秒、内存涨到 591MB，
必须强杀。回环链条：

    StreamHandler.emit 写控制台失败
      → logging.handleError → sys.stderr.write('--- Logging error ---\n')
        → sys.stderr 正是 LoggerStream → LoggerStream.write
          → logger.error → handler 再次失败 → handleError → …

``LoggerStream.write`` 里的 ``try/except: pass`` 拦不住：递归发生在
``logger.error()`` **内部**，异常不会向外抛。

本测试用一个必定失败的 console handler 复现该链条，断言不会出现重入。
"""
import io
import logging

import pytest

from utils.io_redirect import LoggerStream, redirect_stdio_to_logger, restore_stdio


class _UnencodableStream:
    """模拟 GBK 控制台：遇到 emoji / 生僻字直接抛 UnicodeEncodeError。"""

    def write(self, s):
        s.encode("gbk")

    def flush(self):
        pass


class _ExplodingHandler(logging.StreamHandler):
    """真·StreamHandler：emit 内部捕获异常并调用 handleError（事故的真实形态）。"""

    def __init__(self):
        super().__init__(_UnencodableStream())
        self.emit_calls = 0

    def emit(self, record):
        self.emit_calls += 1
        super().emit(record)


def _radar_like_logger(name="reentrant-test"):
    """带一个必定失败的 console handler 的 logger（propagate 不参与，与事故一致）。"""
    raw = logging.getLogger(f"_test_{name}")
    raw.handlers.clear()
    raw.propagate = False
    raw.setLevel(logging.INFO)

    class _Wrapper:
        """最小 RadarLogger 替身：LoggerStream 靠 `.info` + `._logger` 判定 _is_radar。"""

        def __init__(self, inner):
            self._logger = inner
            self.records = []

        def error(self, tag, msg=""):
            self.records.append(("error", tag, msg))
            self._logger.error(f"{tag} {msg}")

        def warning(self, tag, msg=""):
            self.records.append(("warning", tag, msg))
            self._logger.warning(f"{tag} {msg}")

        def debug(self, tag, msg=""):
            self.records.append(("debug", tag, msg))
            self._logger.debug(f"{tag} {msg}")

        # _is_radar 探测需要 .info；非 radar 分支会调 .log
        def info(self, tag, msg=""):
            self.records.append(("info", tag, msg))
            self._logger.info(f"{tag} {msg}")

        def log(self, level, msg=""):
            self.records.append((logging.getLevelName(level), "IO", msg))
            self._logger.log(level, msg)

    handler = _ExplodingHandler()
    raw.addHandler(handler)
    return _Wrapper(raw), handler


def test_write_does_not_reenter_logger_when_handler_fails():
    """handler 写失败时，不得因 handleError 回灌 sys.stderr 而无限递归。"""
    logger, handler = _radar_like_logger()
    stream = LoggerStream(logger, logging.ERROR, io.StringIO())

    # 未修复时：这里会无限递归（handleError → sys.stderr.write → logger.error → …）
    stream.write("触发重入 \U0001F4CA\n")

    # 关键断言：emit 只被调用一次（一次失败 + 零次重入）
    assert handler.emit_calls == 1, (
        f"console handler 被调用 {handler.emit_calls} 次，说明发生了重入递归"
    )
    assert len(logger.records) == 1
    assert logger.records[0][0] == "error"


def test_reentrant_write_still_reaches_original_stream():
    """重入路径降级为只写原始流，不丢用户可见输出。"""
    logger, _ = _radar_like_logger("passthrough")
    sink = io.StringIO()
    stream = LoggerStream(logger, logging.ERROR, sink)

    # 先手动置位，模拟正处于 logger.error → handleError 的嵌套 write 中
    from utils.io_redirect import _write_state

    _write_state.in_write = True
    try:
        stream.write("嵌套写入\n")
    finally:
        _write_state.in_write = False

    assert sink.getvalue() == "嵌套写入\n"


def test_normal_write_unaffected():
    """正常路径行为不变：落原始流 + 进 logger。"""
    logger, _ = _radar_like_logger("normal")
    sink = io.StringIO()
    stream = LoggerStream(logger, logging.ERROR, sink)

    stream.write("正常内容\n")

    assert sink.getvalue() == "正常内容\n"
    assert logger.records == [("error", "IO", "正常内容")]


def test_flag_is_released_on_exception():
    """即使 _write_inner 抛异常也必须复位，否则该线程后续所有输出都会静默。"""
    from utils.io_redirect import _write_state

    logger, _ = _radar_like_logger("release")
    stream = LoggerStream(logger, logging.ERROR, io.StringIO())

    def _boom(_data):
        raise RuntimeError("boom")

    stream._write_inner = _boom
    with pytest.raises(RuntimeError):
        stream.write("会抛\n")

    assert not getattr(_write_state, "in_write", False), "重入标志泄漏，后续写入会全部被吞"

    # 复位后应恢复可用
    stream._write_inner = LoggerStream._write_inner.__get__(stream)
    sink = io.StringIO()
    stream.original_stream = sink
    stream.write("复位后\n")
    assert sink.getvalue() == "复位后\n"


def test_flag_is_thread_local():
    """别的线程在 write 中时，本线程仍应正常写。"""
    import threading

    logger, _ = _radar_like_logger("threadlocal")
    from utils.io_redirect import _write_state

    started = threading.Event()
    release = threading.Event()

    def holder():
        _write_state.in_write = True
        started.set()
        release.wait(2.0)
        _write_state.in_write = False

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    started.wait(2.0)
    try:
        sink = io.StringIO()
        stream = LoggerStream(logger, logging.ERROR, sink)
        stream.write("另一线程应正常\n")
        assert sink.getvalue() == "另一线程应正常\n", "哨兵被跨线程污染"
        assert len(logger.records) == 1
    finally:
        release.set()
        t.join(timeout=2.0)


def test_redirect_and_restore_roundtrip():
    """redirect/restore 仍然可用，且不破坏重入保护。"""
    import sys

    logger, _ = _radar_like_logger("roundtrip")
    old_out, old_err = redirect_stdio_to_logger(logger)
    try:
        assert isinstance(sys.stderr, LoggerStream)
        sys.stderr.write("经重定向的输出\n")
    finally:
        restore_stdio(old_out, old_err)
    assert sys.stderr is old_err