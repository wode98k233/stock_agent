"""/data 全数据监控命令单元测试"""
import pytest

from cli.commands.data_cmd import DataCommand
from cli.commands.base import CommandResult


class FakeDBM:
    """模拟 utils.data_monitor 聚合接口。"""

    def __init__(self):
        self.scanned = 0

    def scan(self):
        self.scanned += 1
        return self.overview()

    def overview(self):
        return {"scanned_at": "2026-09-08 09:00", "assets": [
            {"key": "db/x.db", "category": "db", "name": "x.db", "size_bytes": 2048,
             "record_count": 5, "mtime": "2026-09-08", "tables": []}]}

    def trend(self, days):
        return [{"d": "2026-09-01", "category": "db", "size_bytes": 1024, "record_count": 2}]

    def cleanup_preview(self):
        return [{"id": "small_logs", "name": "小日志文件", "count": 3, "size": 1024, "items": []}]

    def cleanup_run(self, rule_ids):
        return {r: {"removed_count": 3, "freed_bytes": 1024} for r in rule_ids}


@pytest.fixture
def cmd(monkeypatch):
    return DataCommand()


@pytest.mark.asyncio
async def test_overview_default(cmd, monkeypatch):
    fake = FakeDBM()
    import utils.data_monitor as real_dbm
    monkeypatch.setattr(real_dbm, "scan", fake.scan)
    monkeypatch.setattr(real_dbm, "overview", fake.overview)
    monkeypatch.setattr(real_dbm, "cleanup_preview", fake.cleanup_preview)
    monkeypatch.setattr(real_dbm, "trend", fake.trend)
    monkeypatch.setattr(real_dbm, "cleanup_run", fake.cleanup_run)
    res = await cmd.execute(None, [], {})
    assert res.ok
    assert "数据总览" in res.message
    assert "TOP5" in res.message


@pytest.mark.asyncio
async def test_scan_subcommand(cmd, monkeypatch):
    import utils.data_monitor as real_dbm
    fake = FakeDBM()
    monkeypatch.setattr(real_dbm, "scan", fake.scan)
    res = await cmd.execute(None, ["scan"], {})
    assert res.ok
    assert fake.scanned == 1
    assert res.data["scanned_at"] == "2026-09-08 09:00"


@pytest.mark.asyncio
async def test_tables_subcommand_missing_arg(cmd):
    res = await cmd.execute(None, ["tables"], {})
    assert not res.ok


@pytest.mark.asyncio
async def test_clean_requires_confirm(cmd, monkeypatch):
    import utils.data_monitor as real_dbm
    fake = FakeDBM()
    monkeypatch.setattr(real_dbm, "cleanup_preview", fake.cleanup_preview)
    res = await cmd.execute(None, ["clean"], {})
    assert res.ok
    assert res.requires_confirm is True
    assert res.pending_action is not None
    # 执行 pending_action（模拟确认）
    result = res.pending_action()
    assert result.ok
    assert "已清理" in result.message


@pytest.mark.asyncio
async def test_trend_subcommand(cmd, monkeypatch):
    import utils.data_monitor as real_dbm
    fake = FakeDBM()
    monkeypatch.setattr(real_dbm, "trend", fake.trend)
    res = await cmd.execute(None, ["trend"], {})
    assert res.ok
    assert "近 7 天趋势" in res.message