"""端到端：groups dashboard → signal 表 → 主样本/条件桶口径。"""
import sqlite3

import pytest

from signal_eval.db import init_db
from signal_eval.extract import _insert_signal, _signals_items

DASH = {
    "decision_type": "buy", "confidence_level": 0.62, "sentiment_score": 68,
    "core_verdict": "科技/消费/医疗三方向整体可参与",
    "groups": [
        {"key": "科技", "horizon": "1-5天", "decision": "buy", "confidence": 0.66,
         "signal_symbol": "159516", "entry": {"type": "pullback", "low": 0.46, "high": 0.47},
         "invalidation": "破0.456"},
        {"key": "消费", "horizon": "1-2个月", "decision": "buy", "confidence": 0.55,
         "signal_symbol": "159928", "entry": {"type": "breakout", "low": 0.69},
         "invalidation": "破0.676"},
        {"key": "医疗", "horizon": "1-2年", "decision": "buy", "confidence": 0.70,
         "signal_symbol": "513060", "entry": {"type": "pullback", "high": 0.50},
         "invalidation": "破0.485"},
    ],
}


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "signal_eval.db"
    init_db(str(db))
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_flow_three_groups_three_signals(conn):
    row = {"id": 645, "dialog_uuid": "d1", "created_at": "2026-11-04T13:25:41Z"}
    added = sum(_insert_signal(conn, row, it) for it in _signals_items(DASH, ""))
    conn.commit()
    assert added == 3

    rows = conn.execute("SELECT * FROM signal ORDER BY symbol_key").fetchall()
    assert [r["symbol_key"] for r in rows] == ["sh513060", "sz159516", "sz159928"]
    assert all(r["conditional"] == 1 for r in rows)
    assert all(r["scope"] == "symbol" for r in rows)
    assert not conn.execute(
        "SELECT 1 FROM signal WHERE symbol_key='sz159928' AND group_key='' ").fetchone()

    main = conn.execute(
        "SELECT COUNT(*) FROM signal WHERE symbol_key IS NOT NULL AND is_market_wide=0 "
        "AND IFNULL(conditional,0)=0 AND IFNULL(scope,'symbol')='symbol'").fetchone()[0]
    assert main == 0
