"""mx_zixuan 的 Agent 运行契约测试。"""
from unittest.mock import Mock

import pytest
import requests


def test_query_network_error_raises_normal_exception(monkeypatch):
    from tools.other_skills.eastmoney.mx_zixuan.mx_zixuan import query_self_select

    monkeypatch.setattr(
        "tools.other_skills.eastmoney.mx_zixuan.mx_zixuan.requests.post",
        Mock(side_effect=requests.Timeout("timeout")),
    )

    with pytest.raises(Exception, match="查询自选股失败"):
        query_self_select("test-key")


def test_manage_network_error_raises_normal_exception(monkeypatch):
    from tools.other_skills.eastmoney.mx_zixuan.mx_zixuan import manage_self_select

    monkeypatch.setattr(
        "tools.other_skills.eastmoney.mx_zixuan.mx_zixuan.requests.post",
        Mock(side_effect=requests.ConnectionError("offline")),
    )

    with pytest.raises(Exception, match="操作自选股失败"):
        manage_self_select("test-key", "添加贵州茅台")


def test_query_invalid_json_raises_normal_exception(monkeypatch):
    from tools.other_skills.eastmoney.mx_zixuan.mx_zixuan import query_self_select

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("invalid json")
    monkeypatch.setattr(
        "tools.other_skills.eastmoney.mx_zixuan.mx_zixuan.requests.post",
        Mock(return_value=response),
    )

    with pytest.raises(Exception, match="查询自选股失败"):
        query_self_select("test-key")


def test_execute_operation_returns_structured_error(monkeypatch):
    from config import Config
    from tools.other_skills.eastmoney.mx_zixuan.mx_zixuan import execute_zixuan_operation

    monkeypatch.setattr(Config, "MX_APIKEY", "test-key")
    monkeypatch.setattr(
        "tools.other_skills.eastmoney.mx_zixuan.mx_zixuan.requests.post",
        Mock(side_effect=requests.Timeout("timeout")),
    )

    result = execute_zixuan_operation("查询我的自选股列表")

    assert result["success"] is False
    assert "查询自选股失败" in result["error"]


def test_execute_operation_keeps_success_payload(monkeypatch):
    from config import Config
    from tools.other_skills.eastmoney.mx_zixuan.mx_zixuan import execute_zixuan_operation

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": 0, "data": {}}
    monkeypatch.setattr(Config, "MX_APIKEY", "test-key")
    monkeypatch.setattr(
        "tools.other_skills.eastmoney.mx_zixuan.mx_zixuan.requests.post",
        Mock(return_value=response),
    )

    result = execute_zixuan_operation("查询我的自选股列表")

    assert result == {
        "success": True,
        "action": "query",
        "result": {"status": 0, "data": {}},
    }

