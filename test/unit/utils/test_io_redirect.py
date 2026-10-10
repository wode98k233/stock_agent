"""io_redirect：进度条静音（tqdm 写入 stderr 不再刷 ERROR）。"""
import io
import logging

from utils.io_redirect import LoggerStream, _is_progress_bar


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
