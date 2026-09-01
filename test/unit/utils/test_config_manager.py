"""
测试目标: utils/config_manager.py
覆盖范围:
  - ConfigManager 单例
  - generate_diff: 变更差异
  - write_env_changes: 文件写入
  - reload: 刷新 Config
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def cm():
    """每次创建新实例（重置单例）"""
    from utils.config_manager import create_config_manager
    return create_config_manager()


# ── 单例 ─────────────────────────────────────────────────────

class TestSingleton:
    """ConfigManager 单例行为"""

    def test_same_instance(self, cm):
        from utils.config_manager import ConfigManager
        cm2 = ConfigManager()
        assert cm is cm2

    def test_create_config_manager_resets(self, cm):
        from utils.config_manager import create_config_manager
        cm2 = create_config_manager()
        assert cm2 is not None


# ── generate_diff ────────────────────────────────────────────

class TestGenerateDiff:
    """generate_diff: 变更差异（直接读 Config 类属性）"""

    def test_detects_change(self, cm):
        from config import Config
        diff = cm.generate_diff({"LOG_LEVEL": "ERROR"})
        # 如果当前不是 ERROR，应检测到变更
        if Config.LOG_LEVEL != "ERROR":
            assert len(diff) >= 1
            assert diff[0]["key"] == "LOG_LEVEL"
        else:
            assert len(diff) == 0

    def test_no_change_empty_diff(self, cm):
        from config import Config
        diff = cm.generate_diff({"LOG_LEVEL": Config.LOG_LEVEL})
        assert len(diff) == 0

    def test_unknown_key_skipped(self, cm):
        diff = cm.generate_diff({"NONEXISTENT_KEY": "value"})
        assert len(diff) == 0


# ── write_env_changes ────────────────────────────────────────

class TestWriteEnvChanges:
    """write_env_changes: 文件写入"""

    def test_writes_new_key(self, cm, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nEXISTING=value\n")
        from utils.config_manager import ConfigManager
        result = ConfigManager.write_env_changes({"NEW_KEY": "new_value"}, str(env_file))
        assert result is True
        content = env_file.read_text()
        assert "NEW_KEY=new_value" in content
        assert "# comment" in content  # 注释保留

    def test_updates_existing_key(self, cm, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=old\n")
        from utils.config_manager import ConfigManager
        ConfigManager.write_env_changes({"KEY": "new"}, str(env_file))
        content = env_file.read_text()
        assert "KEY=new" in content
        assert "KEY=old" not in content

    def test_bool_value_lowercase(self, cm, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        from utils.config_manager import ConfigManager
        ConfigManager.write_env_changes({"FLAG": True}, str(env_file))
        content = env_file.read_text()
        assert "FLAG=true" in content


# ── reload ───────────────────────────────────────────────────

class TestReload:
    """reload: 委托给 Config.reload()"""

    def test_reload_returns_set(self, cm):
        result = cm.reload()
        assert isinstance(result, set)
