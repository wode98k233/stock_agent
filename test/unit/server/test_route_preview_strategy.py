"""R1 发送前预览：route-preview 的策略三态字段（设计文档 §5.2.1，Plan 2 任务 10）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import Config
from server.routes.report_templates import _strategy_preview, router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_plain_question_no_strategy():
    """回归项：普通提问不能被误判成策略（主流量零成本）"""
    got = _strategy_preview("茅台最近怎么样", "")
    assert got["enabled"] is True
    assert got["is_strategy"] is False
    assert got["disambiguate"] is False
    assert got["session_active"] is False


def test_strategy_input_flagged():
    """策略输入命中：与闸门同源（detect_strategy_intent）"""
    got = _strategy_preview("尾盘买入第二天卖出，这个策略如何", "")
    assert got["is_strategy"] is True
    assert got["reason"] == "explicit_imperative"
    assert got["confidence"] >= 0.5
    assert got["signals"].get("condition_words") or got["signals"].get("imperatives")


def test_mode_off_disables(monkeypatch):
    """STRATEGY_INTENT_MODE=off ⇒ 预览条彻底不参与（不误伤、不提示）"""
    monkeypatch.setattr(Config, "STRATEGY_INTENT_MODE", "off")
    got = _strategy_preview("尾盘买入第二天卖出，这个策略如何", "dlg-1")
    assert got["enabled"] is False
    assert got["mode"] == "off"
    assert got["is_strategy"] is False
    assert got["session_active"] is False


def test_session_active(monkeypatch):
    """会话态：dialog_uuid 非空才查库；空串不许查库"""
    calls = []

    def _fake_get_open(dialog_uuid):
        calls.append(dialog_uuid)
        return {"run_uuid": "sr-x"}

    monkeypatch.setattr("utils.cache.backtest_db.get_open_strategy_session", _fake_get_open)
    assert _strategy_preview("随便说点什么", "dlg-1")["session_active"] is True
    assert calls == ["dlg-1"]
    assert _strategy_preview("随便说点什么", "")["session_active"] is False
    assert calls == ["dlg-1"]  # 空串没有触发查询


def test_route_adds_field():
    """加性字段：既有 ranked / winner 契约不变"""
    res = _client().post("/api/report-templates/route-preview", json={"text": "茅台最近怎么样"})
    assert res.status_code == 200
    body = res.json()
    assert "ranked" in body and "winner" in body
    assert body["strategy"]["enabled"] is True
    assert body["strategy"]["is_strategy"] is False


def test_route_accepts_dialog_uuid():
    """请求体带 dialog_uuid 不报错（加性参数）"""
    res = _client().post("/api/report-templates/route-preview",
                         json={"text": "MACD 是什么", "dialog_uuid": "dlg-9"})
    assert res.status_code == 200
    body = res.json()
    assert body["strategy"]["disambiguate"] is True
