"""
缓存模块单元测试

验证缓存系统的核心逻辑：
1. 市场时间判断
2. TTL 过期策略
3. DataFrame 序列化/反序列化
4. 通用 CRUD 读写
5. 清理器注册与执行

运行方式：
  pytest test/unit/test_cache.py -v
"""
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd

# 确保项目根目录在 sys.path 中
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ============================================================
# 1. 市场时间判断
# ============================================================

def test_market_closed_weekend():
    """周末应该判定为已收盘"""
    from utils.cache.market import is_market_closed
    # 2026-05-09 是周六
    with patch("utils.cache.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 9, 10, 0, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert is_market_closed() == True


def test_market_closed_weekday_after_15():
    """工作日 15:00 后应该判定为已收盘"""
    from utils.cache.market import is_market_closed
    with patch("utils.cache.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 8, 15, 30, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert is_market_closed() == True


def test_market_open_weekday_before_15():
    """工作日 15:00 前应该判定为未收盘"""
    from utils.cache.market import is_market_closed
    with patch("utils.cache.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 8, 14, 30, 0)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert is_market_closed() == False


def test_next_trading_open_time_monday():
    """周六计算下一个交易日应该是下周一 09:30"""
    from utils.cache.market import _get_next_trading_open_time
    with patch("utils.cache.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 9, 16, 0, 0)  # 周六
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _get_next_trading_open_time()
        assert result.weekday() == 0  # 周一
        assert result.hour == 9
        assert result.minute == 30


def test_next_trading_open_time_friday_evening():
    """周五晚上计算下一个交易日应该是下周一 09:30"""
    from utils.cache.market import _get_next_trading_open_time
    with patch("utils.cache.market.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 8, 20, 0, 0)  # 周五
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _get_next_trading_open_time()
        assert result.weekday() == 0  # 周一
        assert result.hour == 9
        assert result.minute == 30


# ============================================================
# 2. TTL 过期策略
# ============================================================

def test_policy_trading_hours():
    """交易时间内应返回 trading TTL"""
    from utils.cache.policies import _get_expire_hours
    with patch("utils.cache.policies.is_market_closed", return_value=False):
        assert _get_expire_hours("history") == 0.25
        assert _get_expire_hours("general") == 0.5
        assert _get_expire_hours("news") == 1.0
        assert _get_expire_hours("financial") == 2.0
        assert _get_expire_hours("valuation_history") == 168


def test_policy_closed_hours_news():
    """收盘后 news 应返回固定 4 小时"""
    from utils.cache.policies import _get_expire_hours
    with patch("utils.cache.policies.is_market_closed", return_value=True):
        assert _get_expire_hours("news") == 4.0


def test_policy_closed_hours_same():
    """收盘后 valuation_history 应返回 same (168)"""
    from utils.cache.policies import _get_expire_hours
    with patch("utils.cache.policies.is_market_closed", return_value=True):
        assert _get_expire_hours("valuation_history") == 168
        assert _get_expire_hours("risk_metrics") == 168


def test_policy_closed_hours_next_open():
    """收盘后 general 应返回到下次开盘的小时数（>= 1）"""
    from utils.cache.policies import _get_expire_hours
    with patch("utils.cache.policies.is_market_closed", return_value=True):
        result = _get_expire_hours("general")
        assert result >= 1


def test_policy_parent_delegation():
    """rating 和 board 应委托给 general"""
    from utils.cache.policies import _get_expire_hours
    with patch("utils.cache.policies.is_market_closed", return_value=False):
        assert _get_expire_hours("rating") == _get_expire_hours("general")
        assert _get_expire_hours("board") == _get_expire_hours("general")


def test_policy_unknown_type():
    """未知缓存类型应抛出 ValueError"""
    from utils.cache.policies import _get_expire_hours
    try:
        _get_expire_hours("nonexistent")
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "未知缓存类型" in str(e)


# ============================================================
# 3. DataFrame 序列化/反序列化
# ============================================================

def test_df_roundtrip_basic():
    """DataFrame 序列化后反序列化应保持数据"""
    from utils.cache.core import _df_to_cache, _cache_to_df

    df = pd.DataFrame({"close": [100.0, 101.5], "volume": [1000, 2000]})
    serialized = _df_to_cache(df)
    result = _cache_to_df(serialized)

    assert isinstance(result, pd.DataFrame)
    assert list(result.columns) == ["close", "volume"]
    assert result["close"].tolist() == [100.0, 101.5]


def test_df_roundtrip_with_datetime_index():
    """带日期索引的 DataFrame 应正确反序列化"""
    from utils.cache.core import _df_to_cache, _cache_to_df

    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    df = pd.DataFrame({"close": [100.0, 101.5]}, index=dates)
    df.index.name = "date"

    serialized = _df_to_cache(df)
    result = _cache_to_df(serialized)

    assert isinstance(result, pd.DataFrame)
    assert result.index.name == "date"


def test_cache_to_df_non_dataframe():
    """非 DataFrame 格式的 JSON 应原样返回"""
    from utils.cache.core import _cache_to_df

    data = {"key": "value"}
    result = _cache_to_df(json.dumps(data))
    assert result == data


# ============================================================
# 4. 通用 CRUD 读写（使用 in-memory DB）
# ============================================================

def _setup_memory_db():
    """创建 in-memory SQLite 并初始化表"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS cache_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_key TEXT UNIQUE NOT NULL,
        data TEXT NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        expire_hours REAL NOT NULL
    )''')
    conn.commit()
    return conn


def test_cache_set_and_get():
    """写入后读取应返回相同数据"""
    from utils.cache.core import _set, _get

    conn = _setup_memory_db()
    with patch("utils.cache.core.get_db") as mock_get_db:
        mock_get_db.return_value.__enter__ = lambda s: conn
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        _set("cache_news", "test_key", {"price": 100}, expire_hours=1)
        result = _get("cache_news", "test_key")
        assert result == {"price": 100}
    conn.close()


def test_cache_get_nonexistent():
    """读取不存在的 key 应返回 None"""
    from utils.cache.core import _get

    conn = _setup_memory_db()
    with patch("utils.cache.core.get_db") as mock_get_db:
        mock_get_db.return_value.__enter__ = lambda s: conn
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        result = _get("cache_news", "nonexistent")
        assert result is None
    conn.close()


def test_cache_expired_entry():
    """过期条目应返回 None 并被删除"""
    from utils.cache.core import _set, _get

    conn = _setup_memory_db()
    with patch("utils.cache.core.get_db") as mock_get_db:
        mock_get_db.return_value.__enter__ = lambda s: conn
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        # 写入一个已过期的条目（1 小时前写入，TTL 0.5 小时）
        old_time = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO cache_news (cache_key, data, updated_at, expire_hours) VALUES (?, ?, ?, ?)",
            ("old_key", json.dumps({"old": True}), old_time, 0.5)
        )
        conn.commit()

        with patch("utils.cache.core.is_market_closed", return_value=False):
            result = _get("cache_news", "old_key")
        assert result is None
    conn.close()


def test_cache_market_aware_not_expired():
    """当天更新 + 市场已收盘 = 不过期（市场感知）"""
    from utils.cache.core import _get

    conn = _setup_memory_db()
    # 写入一个按 TTL 已过期但当天更新的条目
    today_str = datetime.now().strftime('%Y-%m-%d')
    old_time = f"{today_str} 10:00:00"
    conn.execute(
        "INSERT INTO cache_news (cache_key, data, updated_at, expire_hours) VALUES (?, ?, ?, ?)",
        ("today_key", json.dumps({"today": True}), old_time, 0.25)
    )
    conn.commit()

    with patch("utils.cache.core.get_db") as mock_get_db:
        mock_get_db.return_value.__enter__ = lambda s: conn
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        # 市场已收盘 + 当天更新 → 不过期
        with patch("utils.cache.core.is_market_closed", return_value=True):
            result = _get("cache_news", "today_key")
        assert result == {"today": True}
    conn.close()


# ============================================================
# 5. 清理器注册与执行
# ============================================================

def test_cleaner_registry_register():
    """注册清理器后应出现在列表中"""
    from utils.cache.cleaners import CacheCleanerRegistry

    # 保存原始列表
    original = CacheCleanerRegistry._cleaners[:]

    mock_cleaner = MagicMock()
    mock_cleaner.name = "test_cleaner"
    mock_cleaner.clean.return_value = 5

    CacheCleanerRegistry.register(mock_cleaner)
    assert mock_cleaner in CacheCleanerRegistry._cleaners

    # 恢复
    CacheCleanerRegistry._cleaners = original


def test_cleaner_registry_clean_all():
    """clean_all 应调用所有注册的清理器"""
    from utils.cache.cleaners import CacheCleanerRegistry

    original = CacheCleanerRegistry._cleaners[:]

    mock1 = MagicMock()
    mock1.name = "cleaner1"
    mock1.clean.return_value = 3

    mock2 = MagicMock()
    mock2.name = "cleaner2"
    mock2.clean.return_value = 7

    CacheCleanerRegistry._cleaners = [mock1, mock2]
    results = CacheCleanerRegistry.clean_all()

    assert results["cleaner1"] == 3
    assert results["cleaner2"] == 7
    mock1.clean.assert_called_once()
    mock2.clean.assert_called_once()

    CacheCleanerRegistry._cleaners = original


def test_cleaner_registry_error_handling():
    """清理器异常不应影响其他清理器"""
    from utils.cache.cleaners import CacheCleanerRegistry

    original = CacheCleanerRegistry._cleaners[:]

    mock_fail = MagicMock()
    mock_fail.name = "failing"
    mock_fail.clean.side_effect = Exception("boom")

    mock_ok = MagicMock()
    mock_ok.name = "ok"
    mock_ok.clean.return_value = 10

    CacheCleanerRegistry._cleaners = [mock_fail, mock_ok]
    results = CacheCleanerRegistry.clean_all()

    assert results["failing"] == -1
    assert results["ok"] == 10

    CacheCleanerRegistry._cleaners = original


def test_utils_cache_cleaner_name():
    """UtilsCacheCleaner 的 name 属性"""
    from utils.cache.cleaners import UtilsCacheCleaner
    cleaner = UtilsCacheCleaner()
    assert cleaner.name == "utils_cache"


def test_dialog_cleaner_name():
    """DialogCleaner 的 name 属性"""
    from utils.cache.cleaners import DialogCleaner
    cleaner = DialogCleaner()
    assert cleaner.name == "dialog"


def test_logs_cleaner_name():
    """LogsCleaner 的 name 属性"""
    from utils.cache.cleaners import LogsCleaner
    cleaner = LogsCleaner()
    assert cleaner.name == "logs"


if __name__ == "__main__":
    import traceback
    tests = [
        test_market_closed_weekend,
        test_market_closed_weekday_after_15,
        test_market_open_weekday_before_15,
        test_next_trading_open_time_monday,
        test_next_trading_open_time_friday_evening,
        test_policy_trading_hours,
        test_policy_closed_hours_news,
        test_policy_closed_hours_same,
        test_policy_closed_hours_next_open,
        test_policy_parent_delegation,
        test_policy_unknown_type,
        test_df_roundtrip_basic,
        test_df_roundtrip_with_datetime_index,
        test_cache_to_df_non_dataframe,
        test_cache_set_and_get,
        test_cache_get_nonexistent,
        test_cache_expired_entry,
        test_cache_market_aware_not_expired,
        test_cleaner_registry_register,
        test_cleaner_registry_clean_all,
        test_cleaner_registry_error_handling,
        test_utils_cache_cleaner_name,
        test_dialog_cleaner_name,
        test_logs_cleaner_name,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"[PASS] {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test.__name__}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Total: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("[OK] 所有缓存测试通过!")
    print(f"{'='*60}")
    sys.exit(1 if failed > 0 else 0)
