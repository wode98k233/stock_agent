"""数据源优先级自定义顺序单测（系统配置面板拖拽调整的后端契约）。

对应功能：`Config.DATASOURCE_PRIORITY_ORDER`（.env）+ `DataSourceManager.set_order`
+ `PUT/DELETE /api/datasources/order`。
"""
import pytest

pytestmark = pytest.mark.unit


class _FakeSource:
    """最小数据源替身：只需要 name / priority / is_available。"""

    def __init__(self, name, priority):
        self.name = name
        self.priority = priority

    @classmethod
    def is_available(cls):
        return True


@pytest.fixture
def manager():
    from tools.fetcher.base import DataSourceManager

    saved_sources = list(DataSourceManager._sources)
    saved_order = list(DataSourceManager._user_order)
    DataSourceManager._sources = [
        _FakeSource("mx_data", 110),
        _FakeSource("sina_direct", 100),
        _FakeSource("hithink", 95),
        _FakeSource("akshare", 90),
    ]
    DataSourceManager._user_order = []
    yield DataSourceManager
    DataSourceManager._sources = saved_sources
    DataSourceManager._user_order = saved_order


class TestDefaultOrder:
    def test_builtin_priority_order(self, manager):
        manager._resort_sources()
        assert manager.get_order() == ["mx_data", "sina_direct", "hithink", "akshare"]
        assert manager.order_is_customized() is False


class TestCustomOrder:
    def test_set_order_puts_listed_first_then_rest(self, manager):
        applied = manager.set_order(["hithink", "akshare"])
        assert applied == ["hithink", "akshare", "mx_data", "sina_direct"]
        assert manager.order_is_customized() is True

    def test_duplicate_names_deduped(self, manager):
        applied = manager.set_order(["hithink", "hithink", "akshare"])
        assert applied == ["hithink", "akshare", "mx_data", "sina_direct"]

    def test_unknown_names_are_kept_in_order_but_have_no_source(self, manager):
        # set_order 本身不做校验（校验在 API 层）；未知名字不报错、也不影响已注册源顺序
        applied = manager.set_order(["nope", "hithink"])
        assert applied[0] == "hithink"

    def test_clear_order_restores_builtin(self, manager):
        manager.set_order(["akshare", "hithink"])
        assert manager.get_order()[0] == "akshare"
        applied = manager.clear_order()
        assert applied == ["mx_data", "sina_direct", "hithink", "akshare"]
        assert manager.order_is_customized() is False


class TestCapabilityOrderInteraction:
    def test_capability_order_applies_when_not_customized(self, manager):
        manager._resort_sources()
        ordered = manager.order_sources(list(manager._sources), "hist")
        # 内置 hist 顺序为 hithink → efinance → akshare → baostock → sina_direct → mx_data
        # 本次替身只有 4 个源，故生效顺序为 hithink, akshare, sina_direct, mx_data
        assert [s.name for s in ordered] == ["hithink", "akshare", "sina_direct", "mx_data"]

    def test_capability_order_skipped_when_user_customized(self, manager):
        """用户显式顺序优先，能力级重排不得覆盖（避免两套规则打架）。"""
        manager.set_order(["mx_data", "hithink", "akshare", "sina_direct"])
        ordered = manager.order_sources(list(manager._sources), "hist")
        assert [s.name for s in ordered][0] == "mx_data"


class TestLoadFromConfig:
    def test_parses_comma_separated(self, manager, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "DATASOURCE_PRIORITY_ORDER", " hithink , akshare ", raising=False)
        assert manager._load_user_order() == ["hithink", "akshare"]

    def test_empty_returns_empty_list(self, manager, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "DATASOURCE_PRIORITY_ORDER", "", raising=False)
        assert manager._load_user_order() == []
