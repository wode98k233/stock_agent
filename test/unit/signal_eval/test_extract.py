"""信号提取测试：dashboard 解析 + 标的提取 + 增量扫描写库。"""
import json
import sqlite3
import threading
import time

from signal_eval.extract import parse_dashboard, extract_symbol, scan_messages, extract_by_message_uuid


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


def test_extract_symbol_no_code_returns_none(tmp_path, monkeypatch):
    """无代码且无命中时返回 None（隔离本地 stock_info 与 ETF 别名查库）。"""
    info_db = tmp_path / "market_data.db"
    sqlite3.connect(str(info_db)).close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    import signal_eval.symbols as sym
    monkeypatch.setattr("signal_eval.symbols.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.symbols._alias_cache", {})
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


# ── extract_by_message_uuid（P1 hook 入口：agent 回答落库后按消息定向提取） ──

def _make_msg_db(tmp_path, with_uuid_col=True):
    """造消息库（含 message_uuid 列，与真实 web_messages 一致）。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    cols = ("id INTEGER PRIMARY KEY, dialog_uuid TEXT, message_uuid TEXT, "
            "role TEXT, content TEXT, created_at TEXT") if with_uuid_col else \
           ("id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT")
    c.execute(f"CREATE TABLE web_messages ({cols})")
    content = "@@DASHBOARD_START@@{\"decision_type\":\"buy\",\"confidence_level\":\"high\"}@@DASHBOARD_END@@"
    c.execute(
        "INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
        "VALUES('dlg9','mu-001','assistant',?,'2026-09-01T10:00:00')", (content,))
    c.execute(
        "INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
        "VALUES('dlg9','mu-002','user','分析下润泽科技（300442）','2026-09-01T09:00:00')")
    c.commit()
    c.close()
    return msg_db


def test_extract_by_message_uuid(tmp_path, monkeypatch):
    """按 message_uuid 定向提取单条消息 → signal 表。"""
    msg_db = _make_msg_db(tmp_path)
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert extract_by_message_uuid("mu-001") is True
    conn = sqlite3.connect(str(sig_db))
    row = conn.execute("SELECT symbol_key, decision, confidence FROM signal").fetchone()
    conn.close()
    assert row == ("sz300442", "buy", 0.8)


def test_extract_by_message_uuid_idempotent(tmp_path, monkeypatch):
    """同一条消息重复提取（幂等）。"""
    msg_db = _make_msg_db(tmp_path)
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert extract_by_message_uuid("mu-001") is True
    assert extract_by_message_uuid("mu-001") is False


def test_extract_by_message_uuid_missing_returns_false(tmp_path, monkeypatch):
    """不存在的 uuid 安全返回 False（fire-and-forget 不抛错）。"""
    msg_db = _make_msg_db(tmp_path)
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert extract_by_message_uuid("no-such-uuid") is False


def test_extract_by_message_uuid_no_dashboard_returns_false(tmp_path, monkeypatch):
    """无 dashboard 的消息安全返回 False。"""
    msg_db = _make_msg_db(tmp_path)
    c = sqlite3.connect(str(msg_db))
    c.execute(
        "INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
        "VALUES('dlg9','mu-003','assistant','普通回答无仪表盘','2026-09-01T11:00:00')")
    c.commit()
    c.close()
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert extract_by_message_uuid("mu-003") is False


def test_extract_async_swallows_exception(monkeypatch):
    """extract_async（hook 后台入口）：内部异常全吞，不外抛。"""
    from signal_eval.extract import extract_async

    def boom(message_uuid):
        raise RuntimeError("db locked")

    monkeypatch.setattr("signal_eval.extract.extract_by_message_uuid", boom)
    done = threading.Event()

    # 直接同步跑内部线程体验证吞异常（monkeypatch 在子线程可见）
    import signal_eval.extract as ex
    t = threading.Thread(target=lambda: (extract_async("mu-x"), done.set()), daemon=True)
    t.start()
    assert done.wait(2)
    t.join(2)


# ── 多标的（组合仪表盘 symbol_signals 数组） ──

def test_scan_messages_multi_symbol(tmp_path, monkeypatch):
    """symbol_signals 数组 → 逐标的生成多条信号（而非只取第一个标的）。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    dash = json.dumps({
        "decision_type": "hold",
        "core_verdict": "组合持有观察",
        "symbol_signals": [
            {"code": "600900", "name": "长江电力", "decision": "hold", "confidence": 0.65, "condition": "放量跌破26.0止损"},
            {"code": "513330", "name": "恒生互联", "decision": "sell", "confidence": 0.6, "condition": "跌破0.35减仓"},
            {"code": "002714", "name": "牧原股份", "decision": "hold", "confidence": 0.7, "condition": "RSI80以上止盈"},
        ],
    }, ensure_ascii=False)
    content = f"正文\n@@DASHBOARD_START@@{dash}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg3','assistant',?,'2026-09-07T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlg3','user','帮我看看我持仓怎么操作','2026-09-07T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert scan_messages() == 3
    assert scan_messages() == 0   # 幂等：同 message+symbol 不重复
    conn = sqlite3.connect(str(sig_db))
    rows = conn.execute(
        "SELECT symbol_key, symbol_name, decision, confidence FROM signal ORDER BY symbol_key").fetchall()
    conn.close()
    assert rows[0] == ("sh513330", "恒生互联", "sell", 0.6)
    assert rows[1] == ("sh600900", "长江电力", "hold", 0.65)
    assert rows[2] == ("sz002714", "牧原股份", "hold", 0.7)


def test_extract_symbol_resolves_stock_name_real_schema(tmp_path, monkeypatch):
    """真实 schema：stock_info.code 为裸代码（无 s_ 前缀）+ market='SH'/'SZ' → 中文名可解析。

    回归：旧实现查 `code LIKE 's_%'` 恒为 0 行，纯中文名提问的信号被整条丢弃。
    """
    info_db = tmp_path / "market_data.db"
    c = sqlite3.connect(str(info_db))
    c.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT)")
    c.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','SH')")
    c.execute("INSERT INTO stock_info VALUES ('002179','中航光电','SZ')")
    c.commit()
    c.close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(info_db))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    monkeypatch.setattr("signal_eval.extract._name_map", {})
    monkeypatch.setattr("signal_eval.extract._name_map_path", None)
    assert extract_symbol("给我分析下中航光电这个股,未来能走到50吗") == "sz002179"
    assert extract_symbol("贵州茅台怎么走") == "sh600519"


def test_scan_unresolved_goes_unrecognized_idempotent(tmp_path, monkeypatch):
    """标的解析不出（大盘/板块类提问）→ 不再静默丢弃：落 symbol_key=None 进未识别清单，且重扫不重复。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    content = "总结\n@@DASHBOARD_START@@{\"decision_type\":\"hold\",\"core_verdict\":\"大盘弱势观望\"}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgX','assistant',?,'2026-09-08T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgX','user','给我总结下今天盘面情况吧','2026-09-08T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    # 置空真实库路径，避免依赖本机 stock_info
    empty = tmp_path / "market_data.db"
    sqlite3.connect(str(empty)).close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(empty))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    monkeypatch.setattr("signal_eval.extract._name_map", {})
    monkeypatch.setattr("signal_eval.extract._name_map_path", None)

    assert scan_messages() == 1                        # 未识别信号必入库
    assert scan_messages() == 0                        # 重扫幂等，不重复插
    conn = sqlite3.connect(str(sig_db))
    row = conn.execute("SELECT symbol_key, decision, core_verdict, is_market_wide FROM signal").fetchone()
    conn.close()
    assert row == (None, "hold", "大盘弱势观望", 0)


def test_symbol_signals_without_top_decision_extracts(tmp_path, monkeypatch):
    """组合仪表盘缺失顶层 decision_type 时，只要条目自带 decision 仍完整提取（旧实现整条丢弃）。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    dash = json.dumps({
        "core_verdict": "组合调仓",
        "symbol_signals": [
            {"code": "600900", "name": "长江电力", "decision": "buy", "confidence": 0.7},
        ],
    }, ensure_ascii=False)
    content = f"正文\n@@DASHBOARD_START@@{dash}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgY','assistant',?,'2026-09-08T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgY','user','帮我看看持仓','2026-09-08T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    assert scan_messages() == 1
    conn = sqlite3.connect(str(sig_db))
    row = conn.execute("SELECT symbol_key, decision, confidence FROM signal").fetchone()
    conn.close()
    assert row == ("sh600900", "buy", 0.7)


def test_db_migrates_multi_symbol_unique(tmp_path):
    """旧库 UNIQUE(message_uuid) → 迁移为 UNIQUE(message_uuid, symbol_key)，数据保留。"""
    from signal_eval import db as sd
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE signal (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " dialog_uuid TEXT NOT NULL, message_uuid TEXT NOT NULL, symbol_key TEXT,"
        " symbol_name TEXT, decision TEXT NOT NULL, confidence REAL, sentiment REAL,"
        " core_verdict TEXT, signal_time TEXT NOT NULL,"
        " is_market_wide INTEGER NOT NULL DEFAULT 0, UNIQUE(message_uuid))")
    conn.execute("INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time)"
                 " VALUES('d','m1','sh600900','hold','2026-09-07')")
    conn.commit()
    conn.close()

    sd.init_db(path)
    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='signal'").fetchone()[0]
    rows = conn.execute("SELECT symbol_key, decision FROM signal").fetchall()
    conn.close()
    assert "message_uuid, symbol_key" in sql
    assert rows == [("sh600900", "hold")]


# ── 大盘日评自动归档（根因修复：80 条未识别里 50+ 条是"今日大盘总结"类日评） ──

def _make_market_daily_msg_db(tmp_path):
    """造消息库：大盘日评 dashboard（scenario_tag=市场决策仪表盘，含 market_breadth）。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    dash = json.dumps({
        "decision_type": "hold",
        "core_verdict": "大盘弱势观望",
        "scenario_tag": "市场决策仪表盘",
        "market_breadth": {"up": 2000, "down": 3000},
    }, ensure_ascii=False)
    content = f"总结\n@@DASHBOARD_START@@{dash}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgM','assistant',?,'2026-09-08T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgM','user','给我总结下今天盘面情况吧','2026-09-08T09:00:00')")
    c.commit()
    c.close()
    return msg_db


def _isolate_stock_info(tmp_path, monkeypatch):
    """置空 stock_info / ETF 词典来源，隔离本机数据。"""
    empty = tmp_path / "market_data.db"
    sqlite3.connect(str(empty)).close()
    monkeypatch.setattr("signal_eval.extract.STOCK_INFO_DB_PATH", str(empty))
    monkeypatch.setattr("signal_eval.extract._name_cache", {})
    monkeypatch.setattr("signal_eval.extract._name_map", {})
    monkeypatch.setattr("signal_eval.extract._name_map_path", None)
    import signal_eval.symbols as sym
    monkeypatch.setattr("signal_eval.symbols.STOCK_INFO_DB_PATH", str(empty))
    monkeypatch.setattr("signal_eval.symbols._etf_rows_cache", None)
    monkeypatch.setattr("signal_eval.symbols._etf_rows_path", None)


def test_market_daily_dashboard_auto_archived(tmp_path, monkeypatch):
    """大盘日评（无标的类字段+含市场级字段）识别不出标的 → 自动标 is_market_wide=1，不进未识别清单。"""
    msg_db = _make_market_daily_msg_db(tmp_path)
    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    _isolate_stock_info(tmp_path, monkeypatch)

    assert scan_messages() == 1
    conn = sqlite3.connect(str(sig_db))
    row = conn.execute("SELECT symbol_key, is_market_wide FROM signal").fetchone()
    conn.close()
    assert row == (None, 1)


def test_market_daily_judged_by_scenario_tag(tmp_path, monkeypatch):
    """无市场级字段但 scenario_tag 显式声明非个股仪表盘 → 同样判大盘日评。"""
    from signal_eval.extract import _is_market_wide_dash
    assert _is_market_wide_dash({"decision_type": "hold", "scenario_tag": "板块决策仪表盘"})
    assert _is_market_wide_dash({"decision_type": "hold", "scenario_tag": "市场决策仪表盘"})
    # 个股仪表盘且无任何标的字段 → 不判日评（可能只是名称没识别出来，留给人工补标）
    assert not _is_market_wide_dash({"decision_type": "hold", "scenario_tag": "个股决策仪表盘"})
    # 有候选股/标的数组 → 永不判日评
    assert not _is_market_wide_dash({"scenario_tag": "市场决策仪表盘", "candidate_stocks": [{"name": "贵州茅台"}]})
    assert not _is_market_wide_dash({"scenario_tag": "个股决策仪表盘", "symbol_signals": [{"code": "600519"}]})


def test_identified_row_cleans_null_leftover(tmp_path, monkeypatch):
    """同 message 先落了 NULL 行（旧识别失败残留），识别成功后 NULL 行被自动清理。"""
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    content = "@@DASHBOARD_START@@{\"decision_type\":\"buy\",\"confidence_level\":\"medium\"}@@DASHBOARD_END@@"
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgC','assistant',?,'2026-09-08T10:00:00')", (content,))
    c.execute("INSERT INTO web_messages(dialog_uuid,role,content,created_at) VALUES('dlgC','user','看看润泽科技','2026-09-08T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    _isolate_stock_info(tmp_path, monkeypatch)

    # 第一次扫描：空 stock_info 下纯中文名识别不出 → NULL 行入库
    assert scan_messages() == 1
    conn = sqlite3.connect(str(sig_db))
    assert conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 1
    conn.close()

    # 注入名称映射后重扫：识别行入库，同 message 的 NULL 残留行被自动清理
    c = sqlite3.connect(str(tmp_path / "market_data.db"))
    c.execute("CREATE TABLE stock_info (code TEXT, name TEXT, market TEXT)")
    c.execute("INSERT INTO stock_info VALUES ('300442','润泽科技','SZ')")
    c.commit()
    c.close()
    monkeypatch.setattr("signal_eval.extract._name_map", {})
    monkeypatch.setattr("signal_eval.extract._name_map_path", None)
    assert scan_messages() == 1
    conn = sqlite3.connect(str(sig_db))
    rows = conn.execute("SELECT symbol_key FROM signal").fetchall()
    n_null = conn.execute("SELECT COUNT(*) FROM signal WHERE symbol_key IS NULL").fetchone()[0]
    conn.close()
    assert rows == [("sz300442",)]
    assert n_null == 0


def test_backlog_cleanup_removes_null_and_archives_daily(tmp_path, monkeypatch):
    """backlog_cleanup：删同 message 有识别行的 NULL 残留；日评特征 NULL 行归档大盘类。"""
    from signal_eval.extract import backlog_cleanup
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, message_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    # 消息1：个股（user 提问仅中文名，首轮空 stock_info 识别不出）
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d1','mu1','assistant','@@DASHBOARD_START@@{\"decision_type\":\"buy\"}@@DASHBOARD_END@@','2026-09-08T10:00:00')")
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d1','mu0','user','看看润泽科技','2026-09-08T09:00:00')")
    # 消息2：大盘日评（识别不出）
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d2','mu2','assistant',?, '2026-09-08T11:00:00')",
              ("@@DASHBOARD_START@@{\"decision_type\":\"hold\",\"scenario_tag\":\"市场决策仪表盘\",\"index_data\":{\"sh\":1}}@@DASHBOARD_END@@",))
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d2','mu3','user','今天大盘总结','2026-09-08T10:30:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    _isolate_stock_info(tmp_path, monkeypatch)

    scan_messages()  # 全部以 NULL 行落库（stock_info 为空）

    # 模拟旧存量：消息2 的日评 NULL 行未被自动归档（is_market_wide=0，旧行为）
    # + 人工插入同 message 的识别行（模拟"识别逻辑后来修好了"但 NULL 行残留的历史库）。
    # signal.message_uuid 实际存 web_messages.id（extract.py 用 str(row["id"])），消息1 id=1。
    conn = sqlite3.connect(str(sig_db))
    conn.execute("UPDATE signal SET is_market_wide=0 WHERE message_uuid='3'")
    conn.execute("INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,symbol_name,decision,signal_time) "
                 "VALUES('d1','1','sz300442','润泽科技','buy','2026-09-08T10:00:00')")
    conn.commit()
    conn.close()

    result = backlog_cleanup()
    assert result["removed"] >= 1       # 消息1 的 NULL 残留被删
    assert result["archived"] >= 1      # 消息2 的 NULL 行被归档大盘类

    conn = sqlite3.connect(str(sig_db))
    keys = conn.execute("SELECT message_uuid, symbol_key, is_market_wide FROM signal ORDER BY id").fetchall()
    conn.close()
    assert ("1", "sz300442", 0) in keys
    assert ("3", None, 1) in keys
    # 幂等
    assert backlog_cleanup() == {"removed": 0, "archived": 0}


def test_backlog_cleanup_keeps_manual_marks(tmp_path, monkeypatch):
    """人工补标过的 NULL 行（symbol_name 非空）不被清理。"""
    from signal_eval.extract import backlog_cleanup
    msg_db = tmp_path / "stock_radar.db"
    c = sqlite3.connect(str(msg_db))
    c.execute("CREATE TABLE web_messages (id INTEGER PRIMARY KEY, dialog_uuid TEXT, message_uuid TEXT, role TEXT, content TEXT, created_at TEXT)")
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d1','mu1','assistant','@@DASHBOARD_START@@{\"decision_type\":\"buy\"}@@DASHBOARD_END@@','2026-09-08T10:00:00')")
    c.execute("INSERT INTO web_messages(dialog_uuid,message_uuid,role,content,created_at) "
              "VALUES('d1','mu0','user','XX标的','2026-09-08T09:00:00')")
    c.commit()
    c.close()

    sig_db = tmp_path / "signal_eval.db"
    monkeypatch.setattr("signal_eval.extract.MSG_DB_PATH", str(msg_db))
    monkeypatch.setattr("signal_eval.extract.SIGNAL_DB_PATH", str(sig_db))
    _isolate_stock_info(tmp_path, monkeypatch)
    scan_messages()

    # 模拟用户人工在 NULL 行上补了 symbol_name（尚未填代码）
    conn = sqlite3.connect(str(sig_db))
    conn.execute("UPDATE signal SET symbol_name='用户手写备注' WHERE symbol_key IS NULL")
    conn.commit()
    conn.close()

    result = backlog_cleanup()
    conn = sqlite3.connect(str(sig_db))
    n = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    conn.close()
    assert result["removed"] == 0
    assert n == 1
