"""_WinSafeTimedRotatingFileHandler 轮转降级回归测试。

背景：web / trace / CLI 多进程常驻打开同一 radar-system.log，Windows 上标准库
TimedRotatingFileHandler 跨天 rename 必抛 PermissionError(WinError 32)，且失败后
rolloverAt 不推进 → 每条日志重试轮转 → stderr 无限刷 "--- Logging error ---"。
本测试锁死三条降级路径：rename 成功 / copytruncate / 彻底放弃不抛异常。
"""
import logging
import os
import time
from pathlib import Path

from utils.logger import _WinSafeTimedRotatingFileHandler


def _make_handler(tmp_path: Path, name: str = "radar-system.log"):
    h = _WinSafeTimedRotatingFileHandler(
        str(tmp_path / name), encoding="utf-8", when="midnight", backupCount=5
    )
    h.setLevel(logging.INFO)
    return h


def _make_logger(h) -> logging.Logger:
    lg = logging.getLogger(f"test_win_safe_rotate.{id(h)}")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


def _force_due(h):
    h.rolloverAt = int(time.time()) - 10


def _cleanup(lg, h):
    lg.removeHandler(h)
    h.close()


def test_rename_success_normal_path(tmp_path):
    """rename 可用时走正常路径：备份含旧内容，新文件只有新记录。"""
    h = _make_handler(tmp_path)
    lg = _make_logger(h)
    try:
        lg.info("before rotate")
        _force_due(h)
        lg.info("trigger rollover")  # 这条触发 doRollover
        lg.info("after rotate")

        backups = list(tmp_path.glob("radar-system.log.*"))
        assert len(backups) == 1
        content = backups[0].read_text(encoding="utf-8")
        assert "before rotate" in content
        assert "trigger rollover" not in content

        current = (tmp_path / "radar-system.log").read_text(encoding="utf-8")
        assert "after rotate" in current
        assert h.rolloverAt > int(time.time()) - 1
    finally:
        _cleanup(lg, h)


def test_rename_locked_falls_back_to_copytruncate(tmp_path, monkeypatch):
    """rename 被占（WinError 32）时降级 copytruncate：备份保留旧内容、当前文件继续写、不抛异常。"""
    h = _make_handler(tmp_path)
    lg = _make_logger(h)
    try:
        lg.info("before rotate")
        _force_due(h)

        def locked_rename(src, dst):
            raise PermissionError(32, "另一个程序正在使用此文件")

        monkeypatch.setattr(os, "rename", locked_rename)
        lg.info("trigger rollover")  # rename 失败 → copytruncate，不应抛异常
        lg.info("after rotate")

        backups = list(tmp_path.glob("radar-system.log.*"))
        assert len(backups) == 1
        backup_content = backups[0].read_text(encoding="utf-8")
        assert "before rotate" in backup_content
        assert "trigger rollover" not in backup_content

        current = (tmp_path / "radar-system.log").read_text(encoding="utf-8")
        assert "after rotate" in current
        assert "before rotate" not in current  # 已被截断
        assert h.rolloverAt > int(time.time()) - 1  # rolloverAt 照常推进（防每条日志重试刷屏）
    finally:
        _cleanup(lg, h)


def test_copytruncate_also_fails_gives_up_gracefully(tmp_path, monkeypatch):
    """连 copytruncate 都失败时静默放弃本次轮转：不抛异常、当前文件继续写、rolloverAt 推进。"""
    h = _make_handler(tmp_path)
    lg = _make_logger(h)
    try:
        lg.info("before rotate")
        _force_due(h)

        monkeypatch.setattr(os, "rename", lambda s, d: (_ for _ in ()).throw(PermissionError(32, "locked")))
        monkeypatch.setattr(
            h, "_rotate_by_copytruncate",
            lambda s, d: (_ for _ in ()).throw(OSError(13, "copy failed")),
        )
        lg.info("trigger rollover")  # 两条路都失败，也绝不能抛
        lg.info("after rotate")

        current = (tmp_path / "radar-system.log").read_text(encoding="utf-8")
        assert "after rotate" in current
        assert h.rolloverAt > int(time.time()) - 1
    finally:
        _cleanup(lg, h)


def test_get_files_to_delete_swallows_locked_backup(tmp_path, monkeypatch):
    """超量备份删除失败不拖垮轮转：getFilesToDelete 吞错并返回空列表。"""
    h = _make_handler(tmp_path)
    lg = _make_logger(h)
    try:
        # 手工制造 7 个过期备份（backupCount=5，候选删除 = 最旧 2 个：10-10 / 10-11）
        for i in range(7):
            p = tmp_path / f"radar-system.log.2026-10-{10 + i:02d}"
            p.write_text(f"old {i}", encoding="utf-8")
        # 模拟最旧文件被占用：只让 10-10 那个删不掉，其余正常删
        real_remove = os.remove
        removed = []

        def flaky_remove(path):
            if str(path).endswith("2026-10-10"):
                raise PermissionError(32, "locked")
            removed.append(path)
            real_remove(path)

        monkeypatch.setattr(os, "remove", flaky_remove)  # 自动还原，不污染其他用例
        result = h.getFilesToDelete()

        assert result == []  # 返回空，doRollover 的删除循环空转不抛
        assert any(p.endswith("2026-10-11") for p in removed)  # 其余候选仍被删
        assert (tmp_path / "radar-system.log.2026-10-10").exists()  # 被占的留着
    finally:
        _cleanup(lg, h)


def test_import_time_root_handler_uses_safe_class():
    """护栏：install_root_handler 必须用安全子类，防止日后改回标准库 handler。"""
    import inspect
    from utils import logger as logger_mod

    src = inspect.getsource(logger_mod.install_root_handler)
    assert "_WinSafeTimedRotatingFileHandler(" in src
    assert "logging.handlers.TimedRotatingFileHandler(" not in src
