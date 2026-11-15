"""io_redirect：进度条静音（tqdm 写入 stderr 不再刷 ERROR）+ Python 告警块去重。"""
import io
import logging

from utils.io_redirect import (
    LoggerStream,
    _is_progress_bar,
    _is_python_warning,
    _warn_seen,
)


class _Cap:
    def __init__(self):
        self.calls = []

    def log(self, level, msg):
        self.calls.append((level, msg))


def test_is_progress_bar_variants():
    assert _is_progress_bar(" 33%|####| 1/3 [00:00<00:01, 1.74it/s]")
    assert _is_progress_bar("  0%|          | 0/3 [00:00<?, ?it/s]\r")
    assert _is_progress_bar("redraw\r")
    assert not _is_progress_bar("真实错误: 数据源失败")
    assert not _is_progress_bar("normal log line")


def test_progress_bar_not_logged_but_kept_on_stream():
    cap = _Cap()
    buf = io.StringIO()
    s = LoggerStream(cap, logging.ERROR, buf)
    s.write(" 33%|####| 1/3 [00:00<00:01, 1.74it/s]")
    assert cap.calls == []          # 未进日志
    assert buf.getvalue() != ""     # 仍写原流（控制台可见）


def test_real_stderr_error_still_logged():
    cap = _Cap()
    s = LoggerStream(cap, logging.ERROR, io.StringIO())
    s.write("真正的错误信息")
    assert cap.calls and cap.calls[0][0] == logging.ERROR


# ── Python 告警块去重（2026-10-15 backtrader utcnow 刷屏事故）──────────


def test_is_python_warning_variants():
    block = ("D:\\x\\cerebro.py:1543: DeprecationWarning: datetime.datetime.utcnow() "
             "is deprecated\n  qstart = datetime.datetime.utcnow()\n")
    assert _is_python_warning(block)
    assert _is_python_warning("<frozen os>:232: ResourceWarning: unclosed database in <sqlite3.Connection>")
    assert _is_python_warning("a.py:1: FutureWarning: foo")
    assert not _is_python_warning("真实错误: 数据源失败")
    assert not _is_python_warning("normal log line")
    assert not _is_python_warning(b"bytes")


def test_warning_block_first_only_then_suppressed():
    _warn_seen.clear()
    cap = _Cap()
    s = LoggerStream(cap, logging.ERROR, io.StringIO())
    block = ("D:\\x\\cerebro.py:1543: DeprecationWarning: utcnow is deprecated\n"
             "  qstart = datetime.datetime.utcnow()\n")
    for _ in range(50):
        s.write(block)
    assert len(cap.calls) == 1
    # 首条降级为 WARNING（不再是 ERROR），且内容含告警头
    assert cap.calls[0][0] == logging.WARNING
    assert "DeprecationWarning" in cap.calls[0][1]


def test_warning_block_periodic_count_line_at_100():
    _warn_seen.clear()
    cap = _Cap()
    s = LoggerStream(cap, logging.ERROR, io.StringIO())
    block = "a.py:9: UserWarning: spam\n  x\n"
    for _ in range(100):
        s.write(block)
    assert [lv for lv, _ in cap.calls] == [logging.WARNING, logging.DEBUG]
    assert "重复 100 次" in cap.calls[1][1]


def test_two_distinct_warning_blocks_each_log_first():
    _warn_seen.clear()
    cap = _Cap()
    s = LoggerStream(cap, logging.ERROR, io.StringIO())
    s.write("a.py:1: UserWarning: w1\n  x\n")
    s.write("b.py:2: UserWarning: w2\n  y\n")
    s.write("a.py:1: UserWarning: w1\n  x\n")
    assert len(cap.calls) == 2
