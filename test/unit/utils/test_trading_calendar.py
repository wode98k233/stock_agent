"""交易日历模块单元测试 — 纯本地，不连外部 API"""
from datetime import date, datetime
from unittest.mock import patch, MagicMock
import pytest


# ---------------------------------------------------------------------------
# helpers: 构造 mock exchange-calendars calendar 对象
# ---------------------------------------------------------------------------

def _make_mock_calendar(sessions: set, prev_session_map: dict = None,
                        next_session_map: dict = None, close_map: dict = None):
    """构造 mock calendar 对象。

    sessions: 所有交易日 (datetime 集合)
    prev_session_map: {session_date: previous_session_date}
    next_session_map: {session_date: next_session_date}
    close_map: {session_date: close_datetime}
    """
    cal = MagicMock()

    def is_session(dt):
        return dt in sessions

    cal.is_session = is_session

    def date_to_session(dt, direction="previous"):
        if direction == "previous":
            if prev_session_map and dt in prev_session_map:
                return prev_session_map[dt]
            # 找 sessions 中 <= dt 的最大值
            candidates = sorted(s for s in sessions if s <= dt)
            return candidates[-1] if candidates else dt
        else:  # next
            if next_session_map and dt in next_session_map:
                return next_session_map[dt]
            candidates = sorted(s for s in sessions if s >= dt)
            return candidates[0] if candidates else dt

    cal.date_to_session = date_to_session

    def previous_session(dt):
        if prev_session_map and dt in prev_session_map:
            return prev_session_map[dt]
        candidates = sorted(s for s in sessions if s < dt)
        return candidates[-1] if candidates else dt

    cal.previous_session = previous_session

    def next_session(dt):
        if next_session_map and dt in next_session_map:
            return next_session_map[dt]
        candidates = sorted(s for s in sessions if s > dt)
        return candidates[0] if candidates else dt

    cal.next_session = next_session

    def session_close(dt):
        if close_map and dt in close_map:
            return close_map[dt]
        # 默认收盘时间
        return datetime(dt.year, dt.month, dt.day, 15, 0)

    cal.session_close = session_close

    return cal


# ---------------------------------------------------------------------------
# 2026-05-29 (周四) 附近的一组 CN 交易日
# ---------------------------------------------------------------------------
_CN_SESSIONS = {
    datetime(2026, 5, 27),  # 周二
    datetime(2026, 5, 28),  # 周三
    datetime(2026, 5, 29),  # 周四
    # 5/30 端午节，休市
    datetime(2026, 6, 1),   # 周一
    datetime(2026, 6, 2),   # 周二
}

_HK_SESSIONS = {
    datetime(2026, 5, 28),
    datetime(2026, 5, 29),
    # 5/30 佛诞/端午，休市
    datetime(2026, 6, 1),
}

_US_SESSIONS = {
    datetime(2026, 5, 27),
    datetime(2026, 5, 28),
    datetime(2026, 5, 29),
    # 5/30 正常交易（美股不过端午）
    datetime(2026, 5, 30),
}


def _patch_xcals():
    """返回 patch 对象，将 xcals.get_calendar 按市场返回不同 mock。"""
    cn_cal = _make_mock_calendar(_CN_SESSIONS)
    hk_cal = _make_mock_calendar(_HK_SESSIONS)
    us_cal = _make_mock_calendar(_US_SESSIONS)

    def get_calendar(code):
        return {"XSHG": cn_cal, "XHKG": hk_cal, "XNYS": us_cal}[code]

    return patch("utils.trading_calendar.xcals", **{"get_calendar": get_calendar})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIsMarketOpen:
    """is_market_open 基本场景。"""

    def test_cn_weekday_open(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            assert is_market_open("cn", date(2026, 5, 29)) is True

    def test_cn_holiday_closed(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            # 5/30 端午节
            assert is_market_open("cn", date(2026, 5, 30)) is False

    def test_cn_weekend_closed(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            # 5/31 周日
            assert is_market_open("cn", date(2026, 5, 31)) is False

    def test_hk_holiday_closed(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            assert is_market_open("hk", date(2026, 5, 30)) is False

    def test_us_normal_open(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            # 美股不过端午
            assert is_market_open("us", date(2026, 5, 30)) is True

    def test_unknown_market_fail_open(self):
        with _patch_xcals():
            from utils.trading_calendar import is_market_open
            assert is_market_open("xx", date(2026, 5, 30)) is True

    @patch("utils.trading_calendar._XCALS_AVAILABLE", False)
    def test_xcals_unavailable_fail_open(self):
        from utils.trading_calendar import is_market_open
        assert is_market_open("cn", date(2026, 1, 1)) is True


class TestGetEffectiveTradingDate:
    """get_effective_trading_date 场景。"""

    def test_holiday_returns_previous_session(self):
        """休市日 → 上一个交易日。"""
        with _patch_xcals():
            from utils.trading_calendar import get_effective_trading_date
            with patch("utils.trading_calendar.get_market_now") as mock_now:
                # 模拟 CN 5/30 端午节上午
                mock_now.return_value = datetime(2026, 5, 30, 10, 0)
                result = get_effective_trading_date("cn")
                assert result == date(2026, 5, 29)

    def test_trading_day_before_close(self):
        """交易日但未收盘 → 上一个交易日。"""
        with _patch_xcals():
            from utils.trading_calendar import get_effective_trading_date
            with patch("utils.trading_calendar.get_market_now") as mock_now:
                # 5/29 周四 10:00，还没收盘
                mock_now.return_value = datetime(2026, 5, 29, 10, 0)
                result = get_effective_trading_date("cn")
                assert result == date(2026, 5, 28)

    def test_trading_day_after_close(self):
        """交易日且已收盘 → 当日。"""
        with _patch_xcals():
            from utils.trading_calendar import get_effective_trading_date
            with patch("utils.trading_calendar.get_market_now") as mock_now:
                # 5/29 周四 16:00，已收盘
                mock_now.return_value = datetime(2026, 5, 29, 16, 0)
                result = get_effective_trading_date("cn")
                assert result == date(2026, 5, 29)

    @patch("utils.trading_calendar._XCALS_AVAILABLE", False)
    def test_xcals_unavailable_returns_local_date(self):
        from utils.trading_calendar import get_effective_trading_date
        with patch("utils.trading_calendar.get_market_now") as mock_now:
            mock_now.return_value = datetime(2026, 5, 30, 10, 0)
            result = get_effective_trading_date("cn")
            assert result == date(2026, 5, 30)


class TestGetOpenMarkets:
    """get_open_markets 全市场扫描。"""

    def test_all_open(self):
        with _patch_xcals():
            from utils.trading_calendar import get_open_markets
            with patch("utils.trading_calendar.get_market_local_date") as mock_date:
                mock_date.return_value = date(2026, 5, 29)
                result = get_open_markets()
                assert result == {"cn": True, "hk": True, "us": True}

    def test_cn_closed_holiday(self):
        with _patch_xcals():
            from utils.trading_calendar import get_open_markets
            with patch("utils.trading_calendar.get_market_local_date") as mock_date:
                mock_date.return_value = date(2026, 5, 30)
                result = get_open_markets()
                assert result["cn"] is False
                assert result["hk"] is False
                assert result["us"] is True


class TestGetMarketStatusText:
    """get_market_status_text 输出格式。"""

    def test_all_open(self):
        with _patch_xcals():
            from utils.trading_calendar import get_market_status_text
            with patch("utils.trading_calendar.get_open_markets") as mock:
                mock.return_value = {"cn": True, "hk": True, "us": True}
                text = get_market_status_text()
                assert "正常开市" in text

    def test_some_closed(self):
        with _patch_xcals():
            from utils.trading_calendar import get_market_status_text
            with patch("utils.trading_calendar.get_open_markets") as mock:
                mock.return_value = {"cn": True, "hk": False, "us": True}
                text = get_market_status_text()
                assert "开市" in text
                assert "休市" in text
                assert "港股" in text


class TestGetNextTradingDate:
    """get_next_trading_date 场景。"""

    def test_normal_next_day(self):
        with _patch_xcals():
            from utils.trading_calendar import get_next_trading_date
            result = get_next_trading_date("cn", date(2026, 5, 28))
            assert result == date(2026, 5, 29)

    def test_skip_holiday(self):
        with _patch_xcals():
            from utils.trading_calendar import get_next_trading_date
            # 5/29 的下一个是 6/1（跳过端午+周末）
            result = get_next_trading_date("cn", date(2026, 5, 29))
            assert result == date(2026, 6, 1)

    @patch("utils.trading_calendar._XCALS_AVAILABLE", False)
    def test_fallback_skip_weekend(self):
        from utils.trading_calendar import get_next_trading_date
        # 周五 → 跳过周末 → 周一
        result = get_next_trading_date("cn", date(2026, 5, 29))
        assert result == date(2026, 6, 1)


class TestGetMarketNow:
    """get_market_now 时区转换。"""

    def test_cn_timezone(self):
        from utils.trading_calendar import get_market_now, MARKET_TIMEZONE
        now = get_market_now("cn")
        assert now.tzinfo is not None
        assert "Asia/Shanghai" in str(now.tzinfo)

    def test_us_timezone(self):
        from utils.trading_calendar import get_market_now
        now = get_market_now("us")
        assert now.tzinfo is not None
        assert "America/New_York" in str(now.tzinfo)

    def test_unknown_market_returns_naive(self):
        from utils.trading_calendar import get_market_now
        now = get_market_now("xx")
        # 无时区信息
        assert now.tzinfo is None
