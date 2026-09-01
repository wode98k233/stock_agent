"""信号提取测试：dashboard 解析 + 标的提取 + 增量扫描写库。"""
import json
import sqlite3

from signal_eval.extract import parse_dashboard, extract_symbol, scan_messages


def test_parse_dashboard_json():
    content = ("正文\n@@DASHBOARD_START@@"
               "{\"core_verdict\":\"看好\",\"decision_type\":\"buy\",\"confidence_level\":\"high\",\"sentiment_score\":72}"
               "@@DASHBOARD_END@@")
    d = parse_dashboard(content)
    assert d["decision_type"] == "buy"
    assert d["core_verdict"] == "看好"


def test_parse_dashboard_no_json_returns_none():
    assert parse_dashboard("没有仪表盘") is None


def test_parse_dashboard_broken_json_returns_none():
    assert parse_dashboard("@@DASHBOARD_START@@{bad json@@DASHBOARD_END@@") is None


def test_extract_symbol_from_fullwidth_paren():
    # assistant 首条消息：全角括号代码
    assert extract_symbol("润泽科技（300442）深度分析") == "sz300442"


def test_extract_symbol_sh_market():
    assert extract_symbol("贵州茅台（600519）分析") == "sh600519"


def test_extract_symbol_no_code_returns_none():
    assert extract_symbol("分析下大盘走势") is None


def test_extract_symbol_by_stock_name(tmp_path, monkeypatch):
    """无代码时用中文名查 stock_info 映射。"""
    info_db = tmp_path / "market_data.db"
    c = sqlite3.connect(str(info_db))
    c.execute("CREATE TABLE stock_info (code TEXT, name TEXT)")
    c.execute("INSERT INTO stock_info VALUES ('sh600519','贵州茅台'), ('sz300442','润泽科技')")
    c.commit()
    c.close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    assert extract_symbol("分析下润泽科技这个股") == "sz300442"
    assert extract_symbol("贵州茅台（600519）") == "sh600519"


def test_scan_messages_extracts_signals(tmp_path, monkeypatch):
    # 造消息库：1 条 buy dashboard + 首条 user 消息提供标的
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    content = "分析\n@@DASHBOARD_START@@{\"decision_type\":\"buy\",\"confidence_level\":\"medium\"}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg1','assistant',?,'2026-08-01T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg1','user','分析下润泽科技（300442）','2026-08-01T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    added = scan_messages()
    assert added == 1
    conn = sqlite3.connect(str(sig_db))
    row = conn.execute("SELECT symbol_key, decision, confidence FROM signal").fetchone()
    assert row[0] == "sz300442"   # symbol_key
    assert row[1] == "buy"        # decision
    assert row[2] == 0.6          # medium → 0.6
    conn.close()


def test_scan_messages_idempotent(tmp_path, monkeypatch):
    """重复扫描不重复入库。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    content = "@@DASHBOARD_START@@{\"decision_type\":\"sell\",\"confidence_level\":\"low\"}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg2','assistant',?,'2026-08-02T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg2','user','看看迈瑞医疗（300760）','2026-08-02T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert scan_messages() == 1
    assert scan_messages() == 0   # 第二次扫描无新增（message_uuid UNIQUE）
    conn = sqlite3.connect(str(sig_db))
    n = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    assert n == 1
    conn.close()
