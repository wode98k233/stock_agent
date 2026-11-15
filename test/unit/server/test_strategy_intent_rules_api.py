"""策略意图词表配置 API（R3，设计文档 §5.2.3 / Plan 2 任务 12）。

⚠️ 本文件会真读写仓库词表 agents/shared/strategy_intent_rules.json ⇒ fixture 先备份原文，
finally 写回并 reload_rules()，绝不让测试污染仓库词表。
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.shared import strategy_intent as si
from server import strategy_intent_service as svc
from server.routes.strategy_intent import router


@pytest.fixture()
def restore_rules():
    """用例结束后把词表恢复原样（含 reload，避免污染后续用例）"""
    original = Path(svc._RULES_PATH).read_bytes()
    try:
        yield
    finally:
        Path(svc._RULES_PATH).write_bytes(original)  # 按字节还原，避免行尾被改写
        si.reload_rules()


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_validate_rejects_and_keeps_file(restore_rules):
    """非法结构 → 400 中文错误清单，且词表文件与 mtime 未被动过"""
    before = Path(svc._RULES_PATH).read_text(encoding="utf-8")
    mtime_before = Path(svc._RULES_PATH).stat().st_mtime
    res = _client().put("/api/strategy-intent/rules", json={"rules": {"imperatives": "x"}})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "imperatives" in detail
    assert Path(svc._RULES_PATH).read_text(encoding="utf-8") == before
    assert Path(svc._RULES_PATH).stat().st_mtime == mtime_before


def test_save_then_hot_reload(restore_rules):
    """合法词表保存 → 200 + 备份存在 + 免重启生效"""
    rules = svc.read_rules()
    assert si.detect_strategy_intent("试跑一下 MACD")["is_strategy"] is False
    rules["imperatives"] = list(rules["imperatives"]) + ["试跑一下"]
    res = _client().put("/api/strategy-intent/rules", json={"rules": rules})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert Path(body["backup"]).exists()
    assert si.detect_strategy_intent("试跑一下 MACD")["is_strategy"] is True


def test_test_endpoint_matches_gate(restore_rules):
    """检测端点与闸门同源：三键与直接调 detect_strategy_intent 相等"""
    text = "MACD 金叉买入靠不靠谱"
    got = _client().post("/api/strategy-intent/test", json={"text": text}).json()
    direct = si.detect_strategy_intent(text)
    assert got["is_strategy"] == direct["is_strategy"]
    assert got["disambiguate"] == direct["disambiguate"]
    assert got["reason"] == direct["reason"]
    assert "hits" in got and "slots" in got


def test_get_rules_returns_raw_file(restore_rules):
    """读接口回盘上真实内容 + 路径 + mtime"""
    body = _client().get("/api/strategy-intent/rules").json()
    on_disk = json.loads(Path(svc._RULES_PATH).read_text(encoding="utf-8"))
    assert body["rules"] == on_disk
    assert body["path"].endswith("strategy_intent_rules.json")
    assert body["mtime"] > 0
