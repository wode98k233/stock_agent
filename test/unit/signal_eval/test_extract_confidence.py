"""A2 confidence / confidence_basis 提取测试：数值直存 / 文本兜底 / 缺失不报错 / 存量回填。

对应 A2 设计 §3.2（extract.py confidence 解析块，所有权归 A2，概要设计 §0.4）。
"""
import json
import sqlite3

from signal_eval.extract import _parse_confidence, _parse_confidence_basis, scan_messages

DASH_TMPL = ("正文\n@@DASHBOARD_START@@{json}@@DASHBOARD_END@@")


# ── _parse_confidence ──

def test_conf_numeric_direct():
    assert _parse_confidence({"confidence_level": 0.55}) == 0.55
    assert _parse_confidence({"confidence_level": 0.65}) == 0.65
    assert _parse_confidence({"confidence_level": 1}) == 1.0


def test_conf_numeric_clamped():
    assert _parse_confidence({"confidence_level": 1.7}) == 1.0
    assert _parse_confidence({"confidence_level": -0.2}) == 0.0


def test_conf_numeric_string():
    """LLM 偶发输出字符串数字（如 "0.65"）也要能落库（AC-1 非空率保障）。"""
    assert _parse_confidence({"confidence_level": "0.65"}) == 0.65
    assert _parse_confidence({"confidence_level": " 0.55 "}) == 0.55


def test_conf_text_map_legacy():
    assert _parse_confidence({"confidence_level": "high"}) == 0.8
    assert _parse_confidence({"confidence_level": "MEDIUM"}) == 0.6
    assert _parse_confidence({"confidence_level": "low"}) == 0.4


def test_conf_missing_or_invalid_returns_none():
    """缺失/空/布尔/无法解析 → None，不报错（AC-6）。"""
    assert _parse_confidence({}) is None
    assert _parse_confidence({"confidence_level": None}) is None
    assert _parse_confidence({"confidence_level": ""}) is None
    assert _parse_confidence({"confidence_level": True}) is None
    assert _parse_confidence({"confidence_level": "很高"}) is None


# ── _parse_confidence_basis ──

def test_basis_list_to_json():
    basis = _parse_confidence_basis({"confidence_basis": ["多源印证", "数据新鲜"]})
    assert json.loads(basis) == ["多源印证", "数据新鲜"]


def test_basis_max_three_items():
    basis = _parse_confidence_basis({"confidence_basis": ["a", "b", "c", "d", "e"]})
    assert len(json.loads(basis)) == 3


def test_basis_invalid_returns_none():
    assert _parse_confidence_basis({}) is None
    assert _parse_confidence_basis({"confidence_basis": None}) is None
    assert _parse_confidence_basis({"confidence_basis": "多源印证"}) is None   # 非 list
    assert _parse_confidence_basis({"confidence_basis": []}) is None          # 空数组
    assert _parse_confidence_basis({"confidence_basis": ["", "  "]}) is None  # 全空白


# ── extract_one 落库路径 ──

def _make_msg_db(tmp_path, dash_json):
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    content = DASH_TMPL.format(json=dash_json)
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg1','assistant',?,'2026-09-01T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg1','user','分析下润泽科技（300442）','2026-09-01T09:00:00')")
    c.commit()
    c.close()
    return msg_db


def _scan(tmp_path, monkeypatch, msg_db):
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    added = scan_messages()
    return sig_db, added


def test_extract_numeric_confidence_and_basis(tmp_path, monkeypatch):
    """数值 confidence + basis 数组 → 全部落库（AC-1 主路径）。"""
    dash = '{"decision_type":"buy","confidence_level":0.65,"confidence_basis":["多源印证","数据新鲜"]}'
    sig_db, added = _scan(tmp_path, monkeypatch, _make_msg_db(tmp_path, dash))
    assert added == 1
    conn = sqlite3.connect(str(sig_db))
    conf, basis = conn.execute("SELECT confidence, confidence_basis FROM signal").fetchone()
    conn.close()
    assert conf == 0.65
    assert json.loads(basis) == ["多源印证", "数据新鲜"]


def test_extract_text_confidence_legacy(tmp_path, monkeypatch):
    dash = '{"decision_type":"sell","confidence_level":"low"}'
    sig_db, _ = _scan(tmp_path, monkeypatch, _make_msg_db(tmp_path, dash))
    conn = sqlite3.connect(str(sig_db))
    conf, basis = conn.execute("SELECT confidence, confidence_basis FROM signal").fetchone()
    conn.close()
    assert conf == 0.4
    assert basis is None


def test_extract_missing_confidence_null_no_error(tmp_path, monkeypatch):
    """dashboard 无 confidence 字段 → 该行 confidence=NULL，不报错（AC-6）。"""
    dash = '{"decision_type":"buy","core_verdict":"看好"}'
    sig_db, added = _scan(tmp_path, monkeypatch, _make_msg_db(tmp_path, dash))
    assert added == 1
    conn = sqlite3.connect(str(sig_db))
    conf, basis = conn.execute("SELECT confidence, confidence_basis FROM signal").fetchone()
    conn.close()
    assert conf is None
    assert basis is None


# ── 存量回填（旧数据 confidence 全 NULL → rescan 补齐）──

def test_backfill_existing_null_confidence(tmp_path, monkeypatch):
    """已存在行（confidence=NULL，模拟旧 108 条）→ 重扫补 confidence/basis，不新增行。"""
    dash = '{"decision_type":"buy","confidence_level":0.75,"confidence_basis":["历史胜率参考"]}'
    msg_db = _make_msg_db(tmp_path, dash)
    sig_db, added = _scan(tmp_path, monkeypatch, msg_db)
    assert added == 1
    # 模拟旧数据：清掉 confidence/basis（行仍在）
    conn = sqlite3.connect(str(sig_db))
    conn.execute("UPDATE signal SET confidence=NULL, confidence_basis=NULL")
    conn.commit()
    conn.close()
    # 重扫 → 回填
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert scan_messages() == 0   # 无新增行
    conn = sqlite3.connect(str(sig_db))
    n = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    conf, basis = conn.execute("SELECT confidence, confidence_basis FROM signal").fetchone()
    conn.close()
    assert n == 1                 # 重跑不重复插行（幂等）
    assert conf == 0.75
    assert json.loads(basis) == ["历史胜率参考"]


def test_backfill_never_overwrites_non_null(tmp_path, monkeypatch):
    """confidence 已有值时重扫不覆盖（WHERE confidence IS NULL 守卫）。"""
    dash = '{"decision_type":"buy","confidence_level":0.9}'
    msg_db = _make_msg_db(tmp_path, dash)
    sig_db, _ = _scan(tmp_path, monkeypatch, msg_db)
    conn = sqlite3.connect(str(sig_db))
    conn.execute("UPDATE signal SET confidence=0.11")
    conn.commit()
    conn.close()
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    scan_messages()
    conn = sqlite3.connect(str(sig_db))
    conf = conn.execute("SELECT confidence FROM signal").fetchone()[0]
    conn.close()
    assert conf == 0.11
