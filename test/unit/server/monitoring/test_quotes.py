"""新浪批量报价封装测试 — mock HTTP 响应文本，纯本地。"""
from unittest.mock import MagicMock

import pytest

from server.monitoring.quotes import SinaBatchQuotes, _to_sina_code


def _sina_line(sina_code, name, open_, pre_close, price, high, low,
               date="2026-09-04", time_="15:00:00"):
    """构造一条标准新浪 A 股行情行（33 字段，10-29 为买卖五档）。"""
    levels = ",".join(["100", "10.00"] * 10)  # 买卖五档各 5 档 ×（量+价）= 20 字段
    return (f'var hq_str_{sina_code}="{name},{open_},{pre_close},{price},'
            f'{high},{low},{price},{price},1000000,50500000,{levels},{date},{time_},00";')


@pytest.fixture
def quotes():
    return SinaBatchQuotes()


class TestToSinaCode:
    def test_pure_codes(self):
        assert _to_sina_code("600519") == "sh600519"
        assert _to_sina_code("000001") == "sz000001"
        assert _to_sina_code("300750") == "sz300750"

    def test_prefixed_passthrough(self):
        assert _to_sina_code("sh600519") == "sh600519"
        assert _to_sina_code("SZ000001") == "sz000001"


class TestFetch:
    def _mock_response(self, text, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.content = text.encode("gbk")
        return resp

    def test_parse_basic_fields(self, quotes, monkeypatch):
        text = "\n".join([
            _sina_line("sh600519", "贵州茅台", 1302.00, 1300.00, 1305.50, 1310.00, 1295.00),
            _sina_line("sz000001", "平安银行", 10.00, 10.10, 10.05, 10.20, 9.98),
        ])
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: self._mock_response(text))

        result = quotes.fetch(["600519", "000001"])
        assert set(result.keys()) == {"600519", "000001"}
        m = result["600519"]
        assert m["name"] == "贵州茅台"
        assert m["price"] == 1305.50
        assert m["pre_close"] == 1300.00
        assert m["high"] == 1310.00
        assert m["low"] == 1295.00
        assert m["date"] == "2026-09-04"

    def test_pct_calculated_from_pre_close(self, quotes, monkeypatch):
        text = _sina_line("sh600519", "贵州茅台", 1302.00, 1300.00, 1305.50, 1310.00, 1295.00)
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: self._mock_response(text))

        m = quotes.fetch(["600519"])["600519"]
        assert m["pct"] == pytest.approx((1305.50 - 1300.00) / 1300.00 * 100)

    def test_referer_header_sent(self, quotes, monkeypatch):
        """新浪反爬要求 Referer 头，缺失会 403。"""
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return self._mock_response(
                _sina_line("sh600519", "贵州茅台", 1, 1, 1, 1, 1))

        monkeypatch.setattr("server.monitoring.quotes.requests.get", fake_get)
        quotes.fetch(["600519"])
        assert "finance.sina.com.cn" in captured["headers"]["Referer"]
        assert "hq.sinajs.cn" in captured["url"]
        assert "sh600519" in captured["url"]

    def test_batching_over_limit(self, quotes, monkeypatch):
        """超过单批上限自动分批，且批间保留最小间隔防封。

        冻结 monotonic 时钟做确定性断言（旧版断言 sleep 请求值 >= MIN_INTERVAL
        在数学上必不成立：_throttle 睡的是「最小间隔 - 已流逝时间」，恒 < 1s）。
        """
        calls = []
        sleeps = []
        clock = [100.0]

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            code = url.split("=")[-1]  # 单只/批
            return self._mock_response(_sina_line(code, "X", 1, 1, 1, 1, 1))

        def fake_monotonic():
            return clock[0]

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds  # 模拟睡眠推进时钟

        monkeypatch.setattr("server.monitoring.quotes.requests.get", fake_get)
        monkeypatch.setattr("server.monitoring.quotes.time.monotonic", fake_monotonic)
        monkeypatch.setattr("server.monitoring.quotes.time.sleep", fake_sleep)

        codes = [f"60{i:04d}" for i in range(SinaBatchQuotes.BATCH_SIZE + 1)]
        quotes.fetch(codes)

        assert len(calls) == 2  # 400 + 1 两批
        # 首批无历史时间戳不节流；第二批距上批 0s，补足整段最小间隔
        assert sleeps == [pytest.approx(SinaBatchQuotes.MIN_INTERVAL_S)]

    def test_empty_codes_no_request(self, quotes, monkeypatch):
        called = []
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: called.append(1) or self._mock_response(""))
        assert quotes.fetch([]) == {}
        assert called == []

    def test_network_error_returns_empty(self, quotes, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            raise ConnectionError("boom")

        monkeypatch.setattr("server.monitoring.quotes.requests.get", fake_get)
        assert quotes.fetch(["600519"]) == {}

    def test_malformed_lines_skipped(self, quotes, monkeypatch):
        text = "\n".join([
            'var hq_str_sh600519="";',  # 空内容
            "garbage line without quotes",
            _sina_line("sz000001", "平安银行", 10.00, 10.10, 10.05, 10.20, 9.98),
        ])
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: self._mock_response(text))
        result = quotes.fetch(["600519", "000001"])
        assert set(result.keys()) == {"000001"}

    def test_suspended_price_zero_pct_none(self, quotes, monkeypatch):
        """停牌股 price=0 时 pct 置 None（避免 0 昨收/异常涨跌幅）。"""
        text = _sina_line("sh600519", "贵州茅台", 0, 1300.00, 0, 0, 0)
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: self._mock_response(text))
        m = quotes.fetch(["600519"])["600519"]
        assert m["price"] == 0.0
        assert m["pct"] is None

    def test_missing_code_in_response(self, quotes, monkeypatch):
        """请求 2 只只返回 1 只：缺失的不在结果中，不报错。"""
        text = _sina_line("sh600519", "贵州茅台", 1, 1, 1, 1, 1)
        monkeypatch.setattr("server.monitoring.quotes.requests.get",
                            lambda *a, **kw: self._mock_response(text))
        result = quotes.fetch(["600519", "000001"])
        assert set(result.keys()) == {"600519"}
