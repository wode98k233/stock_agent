"""妙想公共请求层（tools/other_skills/eastmoney/mx_common.py）单元测试。

覆盖：限流识别（122）、429/122 退避重试、失败后返回最后一次限流响应、全局节流。
"""
from unittest.mock import MagicMock, patch

import pytest

from tools.other_skills.eastmoney import mx_common
from tools.other_skills.eastmoney.mx_data.mx_data import MXData
from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
from tools.other_skills.eastmoney.mx_xuangu.mx_xuangu import MXSelectStock

_orig_throttle = mx_common._throttle


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mx_common, "_throttle", lambda: None)
    monkeypatch.setattr(mx_common, "RETRY_SLEEPS", (0.0, 0.0))
    yield


def _response(payload=None, http=200):
    resp = MagicMock()
    resp.status_code = http
    resp.json.return_value = payload if payload is not None else {"status": 0, "data": {}}
    if http >= 400:
        import requests

        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{http}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestIsRateLimited:
    def test_detects_mx_rate_limit(self):
        assert mx_common.is_rate_limited({"status": 122, "message": "操作过于频繁"}) is True
        assert mx_common.is_rate_limited({"data": {"status_code": 122}}) is True
        assert mx_common.is_rate_limited({"data": {"status_msg": "操作过于频繁"}}) is True
        assert mx_common.is_rate_limited({"status": 0, "message": "ok"}) is False
        assert mx_common.is_rate_limited(None) is False


class TestPostJson:
    def test_success_single_call(self):
        with patch("tools.other_skills.eastmoney.mx_common.requests.post", return_value=_response()) as post:
            result = mx_common.post_json("http://x", {}, {})
        assert post.call_count == 1
        assert result["status"] == 0

    def test_retries_on_business_122_then_succeeds(self):
        responses = [
            _response(payload={"status": 122, "message": "操作过于频繁"}),
            _response(payload={"status": 0, "data": {"ok": 1}}),
        ]
        with patch("tools.other_skills.eastmoney.mx_common.requests.post", side_effect=responses) as post:
            result = mx_common.post_json("http://x", {}, {})
        assert post.call_count == 2
        assert result["status"] == 0

    def test_retries_on_http_429(self):
        responses = [_response(http=429), _response(payload={"status": 0})]
        with patch("tools.other_skills.eastmoney.mx_common.requests.post", side_effect=responses) as post:
            result = mx_common.post_json("http://x", {}, {})
        assert post.call_count == 2
        assert result["status"] == 0

    def test_returns_rate_limited_payload_after_retries_exhausted(self):
        responses = [_response(payload={"status": 122, "message": "操作过于频繁"})] * 3
        with patch("tools.other_skills.eastmoney.mx_common.requests.post", side_effect=responses) as post:
            result = mx_common.post_json("http://x", {}, {})
        assert post.call_count == 3  # 1 次原始 + 2 次重试
        assert result["status"] == 122

    def test_non_429_http_error_raises_immediately(self):
        responses = [_response(http=403)] * 3
        with patch("tools.other_skills.eastmoney.mx_common.requests.post", side_effect=responses) as post:
            with pytest.raises(Exception):
                mx_common.post_json("http://x", {}, {})
        assert post.call_count == 1

    def test_quota_exhausted_does_not_retry(self):
        """额度耗尽必须只打 1 次请求（2026-09-20：原先打 3 次 + 空等 7s）。"""
        payload = {
            "status": 122,
            "message": "您本周的妙想金融数据skill请求的数据量已达到上限，额度将于次周恢复",
        }
        with patch("tools.other_skills.eastmoney.mx_common.requests.post",
                   side_effect=lambda *a, **k: _response(payload=payload)) as post:
            result = mx_common.post_json("http://x", {}, {})
        assert post.call_count == 1, "额度耗尽不应重试"
        assert result["status"] == 122

    def test_frequency_limit_still_retries(self):
        """频控仍要重试（可自愈），不能被额度判定误伤。"""
        payload = {"status": 122, "message": "操作过于频繁"}
        with patch("tools.other_skills.eastmoney.mx_common.requests.post",
                   side_effect=lambda *a, **k: _response(payload=payload)) as post:
            mx_common.post_json("http://x", {}, {})
        assert post.call_count == 3   # 1 次原始 + 2 次重试


class TestAsDict:
    """as_dict：收敛妙想响应里 `"data": null` 的链式访问（修复 66 次 NoneType.get 失败）。"""

    def test_empty_result_returns_empty_dict(self):
        data = {"data": {}}["data"]
        assert mx_common.as_dict(data) == {}

    def test_missing_key_yields_none_not_dict(self):
        # dict.get 的默认值只在键缺失时生效——这正是 bug 的根源
        assert {"data": None}.get("data", {}) is None

    def test_null_payloads_normalized(self):
        assert mx_common.as_dict({"a": 1}) == {"a": 1}
        assert mx_common.as_dict({}) == {}
        for value in (None, [], "s", 0, (), 1.5, False):
            assert mx_common.as_dict(value) == {}


class TestNullShapeRegression:
    """三种 null 形态不得再抛 AttributeError，应降级为空结果 + 可读错误。"""

    NULL_SHAPES = [
        {"status": 0, "message": "", "data": None},
        {"status": 0, "message": "", "data": {"data": None}},
        {"status": 0, "message": "", "data": {"data": {"searchDataResultDTO": None}}},
        {"status": 0, "message": "", "data": {"data": {"allResults": None}}},
        {"status": 0, "message": "", "data": {"data": {"allResults": {"result": None}}}},
        {"status": 0, "message": "", "data": {"data": {"llmSearchResponse": None}}},
        {"status": 0, "message": "", "data": {}},
    ]

    @pytest.mark.parametrize("payload", NULL_SHAPES)
    def test_mx_data_parse_result(self, payload):
        tables, conditions, total, error = MXData.parse_result(payload)
        assert tables == [] and total == 0
        assert error == "接口返回中无 dataTableDTOList"

    @pytest.mark.parametrize("payload", NULL_SHAPES)
    def test_mx_data_format_terminal(self, payload):
        text = MXData.format_terminal(payload, [], 0)
        assert "**查询结果**" in text

    @pytest.mark.parametrize("payload", NULL_SHAPES)
    def test_mx_xuangu_extract_data(self, payload):
        rows, source, error = MXSelectStock.extract_data(payload)
        assert rows == []
        assert "无有效 dataList" in error

    @pytest.mark.parametrize("payload", NULL_SHAPES)
    def test_mx_search_format_pretty(self, payload):
        text = MXSearch.format_pretty(payload)
        assert isinstance(text, str) and text

    @pytest.mark.parametrize("payload", NULL_SHAPES)
    def test_mx_search_extract_content_never_raises(self, payload):
        # extract_content 的契约是「永不抛异常，兜底 dump 原文」
        assert isinstance(MXSearch.extract_content(payload), str)


class TestIsQuotaExhausted:
    """额度耗尽（122 的另一子类）必须与「操作过于频繁」区分开：前者不可重试。"""

    def test_detects_weekly_data_volume_message(self):
        payload = {
            "status": 122,
            "message": "您本周的妙想金融数据skill请求的数据量已达到上限，额度将于次周恢复，"
                       "如需升级套餐额度，请联系客服电话400-620-1818",
        }
        assert mx_common.is_quota_exhausted(payload) is True

    def test_detects_daily_data_volume_message(self):
        payload = {
            "status": 122,
            "message": "您今日的妙想金融数据skill请求的数据量已达到上限，额度将于次日恢复",
        }
        assert mx_common.is_quota_exhausted(payload) is True

    def test_detects_nested_status_msg(self):
        assert mx_common.is_quota_exhausted({"data": {"status_code": 122, "status_msg": "数据量已达到上限"}}) is True

    def test_frequency_limit_is_not_quota(self):
        """频控是可重试的，不能被误判成额度耗尽。"""
        assert mx_common.is_quota_exhausted({"status": 122, "message": "操作过于频繁"}) is False
        assert mx_common.is_quota_exhausted({"data": {"status_msg": "请求频率过高"}}) is False

    def test_other_payloads_not_quota(self):
        for payload in (None, {}, {"status": 0}, {"status": 122}, "text", []):
            assert mx_common.is_quota_exhausted(payload) is False

    def test_quota_also_matches_broad_rate_limited(self):
        """额度耗尽在宽口径 is_rate_limited 下为 True —— 故调用方必须先判额度。"""
        payload = {"status": 122, "message": "数据量已达到上限，额度将于次周恢复"}
        assert mx_common.is_rate_limited(payload) is True
        assert mx_common.is_quota_exhausted(payload) is True


class TestThrottle:
    def test_sleeps_when_within_interval(self, monkeypatch):
        monkeypatch.setattr(mx_common, "_throttle", _orig_throttle)
        sleeps = []
        ticks = iter([100.2, 100.9])
        monkeypatch.setattr(mx_common.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(mx_common.time, "sleep", lambda seconds: sleeps.append(seconds))
        monkeypatch.setattr(mx_common, "_last_request_at", 100.0, raising=False)

        mx_common._throttle()

        assert sleeps and abs(sleeps[0] - 1.3) < 0.01
        assert mx_common._last_request_at == 100.9


# ── 真实额度耗尽形态回归（2026-09-20 实测） ──────────────────────────────
# 关键：顶层 status == 0 / message == "ok"，额度原文只在 data.data.message，
# 且 dataTableDTOList 为空。修复前 _payload_texts 只取顶层 message 与 data.status_msg
# （都不含额度提示）→ is_quota_exhausted 漏判 False → 额度耗尽仍重试 2 次 + 空等 7s，
# 且上层把它错报成「接口返回中无 dataTableDTOList」/「未找到相关资讯」，
# 让 agent 误以为「该问句查不到」而不是「配额没了」。
_REAL_QUOTA_PAYLOAD = {
    "success": True, "status": 0, "code": 0, "message": "ok",
    "data": {
        "requestId": None, "message": "OK", "status": 0, "code": 0,
        "data": {
            "protocolType": "SEARCH_DATA",
            "id": "363a677b-53e4-41a8-81ea-7188050ab2b1",
            "tableTruncated": False,
            "message": "您本周的妙想金融数据skill请求的数据量已达到上限，额度将于次周恢复，"
                       "如需升级套餐额度，请联系客服电话400-620-1818",
            "searchDataResultDTO": {"questionId": "q", "dataTableDTOList": [], "condition": None},
        },
    },
}

# 对照组：真查不到（message 为字符串 "None"）
_REAL_EMPTY_PAYLOAD = {
    "success": True, "status": 0, "code": 0, "message": "ok",
    "data": {
        "message": "OK", "status": 0, "code": 0,
        "data": {
            "message": "None",
            "searchDataResultDTO": {"questionId": "q", "dataTableDTOList": [], "condition": None},
        },
    },
}


class TestRealQuotaShapeRegression:
    """额度判定必须覆盖「顶层 status==0 + data.data.message」这一真实形态。"""

    def test_payload_texts_drills_into_nested_message(self):
        texts = mx_common._payload_texts(_REAL_QUOTA_PAYLOAD)
        assert any("数据量已达到上限" in t for t in texts), texts

    def test_is_quota_exhausted_detects_real_shape(self):
        assert mx_common.is_quota_exhausted(_REAL_QUOTA_PAYLOAD) is True

    def test_quota_message_returns_original_text(self):
        msg = mx_common.quota_message(_REAL_QUOTA_PAYLOAD)
        assert "数据量已达到上限" in msg and "次周恢复" in msg

    def test_quota_message_empty_when_not_quota(self):
        assert mx_common.quota_message(_REAL_EMPTY_PAYLOAD) == ""

    def test_post_json_does_not_retry_real_shape(self):
        """真实形态的额度耗尽也必须只打 1 次（修复前会打 3 次 + 空等 7s）。"""
        with patch("tools.other_skills.eastmoney.mx_common.requests.post",
                   side_effect=lambda *a, **k: _response(payload=_REAL_QUOTA_PAYLOAD)) as post:
            mx_common.post_json("http://x", {}, {})
        assert post.call_count == 1, "额度耗尽不应重试"

    def test_mx_data_error_names_quota(self):
        _, _, _, error = MXData.parse_result(_REAL_QUOTA_PAYLOAD)
        assert "额度耗尽" in error
        assert "dataTableDTOList" not in error

    def test_mx_xuangu_error_names_quota(self):
        _, _, error = MXSelectStock.extract_data(_REAL_QUOTA_PAYLOAD)
        assert "额度耗尽" in error

    def test_mx_search_error_names_quota(self):
        assert "额度耗尽" in MXSearch.format_pretty(_REAL_QUOTA_PAYLOAD)

    def test_non_quota_empty_still_reports_no_data(self):
        """对照组：真查不到不得被误判成额度。"""
        assert mx_common.is_quota_exhausted(_REAL_EMPTY_PAYLOAD) is False
        _, _, _, error = MXData.parse_result(_REAL_EMPTY_PAYLOAD)
        assert error == "接口返回中无 dataTableDTOList"
        _, _, error2 = MXSelectStock.extract_data(_REAL_EMPTY_PAYLOAD)
        assert "额度耗尽" not in error2
