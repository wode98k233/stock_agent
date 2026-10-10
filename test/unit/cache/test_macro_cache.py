"""宏观缓存策略测试 — macro 固定 24h TTL（与交易时段无关）+ KV 读写。"""
from unittest.mock import patch

import pandas as pd
import pytest


class TestMacroPolicy:
    def test_24h_when_trading(self):
        """交易时段：24h（宏观月度/季度数据与盘中状态无关）。"""
        from utils.cache.policies import _get_expire_hours
        with patch("utils.cache.policies.is_market_closed", return_value=False):
            assert _get_expire_hours("macro") == 24.0

    def test_24h_when_closed(self):
        """休市：同样 24h（不用 next_open 语义——数据源月度发布，无需开盘失效）。"""
        from utils.cache.policies import _get_expire_hours
        with patch("utils.cache.policies.is_market_closed", return_value=True):
            assert _get_expire_hours("macro") == 24.0

    def test_unknown_type_still_raises(self):
        from utils.cache.policies import _get_expire_hours
        with pytest.raises(ValueError):
            _get_expire_hours("nope")


class TestMacroCacheKV:
    @pytest.fixture(autouse=True)
    def _temp_cache_db(self, tmp_path):
        """临时 market_cache.db，避免污染真实库。"""
        from unittest.mock import MagicMock, patch
        from utils.cache import market_cache_db
        db_path = str(tmp_path / "market_cache.db")
        mock_config = MagicMock()
        mock_config.get_market_cache_db_path.return_value = db_path
        with patch.object(market_cache_db, "Config", mock_config):
            market_cache_db.init_market_cache_tables()
            yield

    def test_set_get_roundtrip(self):
        from utils.cache.api import get_macro_cache, set_macro_cache
        df = pd.DataFrame({"report_date": ["2026-08-31"], "value": [6.1]})
        set_macro_cache("m2_yoy", df)
        got = get_macro_cache("m2_yoy")
        assert got is not None
        assert got.iloc[0]["value"] == pytest.approx(6.1)

    def test_miss_returns_none(self):
        from utils.cache.api import get_macro_cache
        assert get_macro_cache("never_set_indicator") is None
