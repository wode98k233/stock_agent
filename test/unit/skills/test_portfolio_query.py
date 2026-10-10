"""portfolio_query 技能单测：4 工具 × 正常/未同步/未持仓/空流水诚实标注。

fixture 用 tmp_path 临时库，禁碰真实 stock_radar.db。
"""
import logging
import importlib.util
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest


def _load_skill_main():
    """tools.skills 无 __init__.py（非包，SkillRegister 按文件路径加载）→
    测试同样按路径加载 main.py。"""
    main_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "tools", "skills", "portfolio_query", "main.py")
    spec = importlib.util.spec_from_file_location(
        "portfolio_query_skill_main", main_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_skill_main = _load_skill_main()
PortfolioQuerySkill = _skill_main.PortfolioQuerySkill
build_tools = _skill_main.build_tools


EM_COLS = ("stock_code TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'cn',"
           " stock_name TEXT, qty REAL NOT NULL DEFAULT 0,"
           " avg_cost REAL NOT NULL DEFAULT 0, realized_pnl REAL DEFAULT 0,"
           " today_pnl REAL,"
           " opened_at TEXT, updated_at TEXT NOT NULL,"
           " source TEXT NOT NULL DEFAULT 'manual', avail_qty REAL,"
           " etf_flag INTEGER NOT NULL DEFAULT 0, UNIQUE(stock_code, market)")
TRADE_COLS = ("id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " trade_uuid TEXT UNIQUE NOT NULL, stock_code TEXT NOT NULL,"
              " stock_name TEXT, market TEXT NOT NULL DEFAULT 'cn',"
              " side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,"
              " trade_date TEXT NOT NULL, fee REAL DEFAULT 0,"
              " source TEXT NOT NULL DEFAULT 'manual', signal_id INTEGER,"
              " dialog_uuid TEXT, remark TEXT, created_at TEXT NOT NULL")


def _mk_db(tmp_path, with_positions=True, with_trades=False, db_file=None):
    db = tmp_path / (db_file or "test_radar.db")
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE IF NOT EXISTS portfolio_position ({EM_COLS})")
    conn.execute(f"CREATE TABLE IF NOT EXISTS portfolio_trade ({TRADE_COLS})")
    if with_positions:
        now = "2026-09-06T12:00:00+00:00"
        rows = [
            ("002714", "cn", "牧原股份", 100, 41.132, 0, now, now,
             "eastmoney", 100, 0),
            ("513330", "cn", "恒生互联ETF", 52500, 0.409, 0, now, now,
             "eastmoney", 52500, 1),
            ("600519", "cn", "贵州茅台", 10, 1620.0, 0, now, now,
             "manual", None, 0),
        ]
        conn.executemany(
            "INSERT INTO portfolio_position(stock_code, market, stock_name,"
            " qty, avg_cost, realized_pnl, opened_at, updated_at, source,"
            " avail_qty, etf_flag) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    if with_trades:
        conn.execute(
            "INSERT INTO portfolio_trade(trade_uuid, stock_code, stock_name,"
            " side, qty, price, trade_date, source, created_at)"
            " VALUES('ptr_t1','600519','贵州茅台','buy',10,1620.0,"
            "'2026-09-01','manual','2026-09-01T01:00:00+00:00')")
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def skill(tmp_path):
    db = _mk_db(tmp_path)
    s = PortfolioQuerySkill(logging.getLogger("test"), None)
    # 注入临时库路径（绕过真实 stock_radar.db）
    s._get_positions = lambda source="", logger=None: __import__(
        "tools.portfolio_data", fromlist=["x"]).get_positions(
        source, logger, db_path=db)
    s._get_position_detail = lambda code, logger=None: __import__(
        "tools.portfolio_data", fromlist=["x"]).get_position_detail(
        code, logger, db_path=db)
    s._get_pnl_summary = lambda logger=None: __import__(
        "tools.portfolio_data", fromlist=["x"]).get_pnl_summary(logger, db_path=db)
    s._get_trade_history = lambda code="", limit=50, logger=None: __import__(
        "tools.portfolio_data", fromlist=["x"]).get_trade_history(
        code, limit, logger, db_path=db)
    return s


def _unwrap(result):
    """skill_tool 包装后返回 JSON 字符串 → 反序列化。"""
    import json
    return json.loads(result) if isinstance(result, str) else result


# ---------------------------------------------------------------------------
# 工具注册（SkillBuilder 模式验证）
# ---------------------------------------------------------------------------

class TestSkillRegistration:
    def test_build_tools_4(self):
        tools = build_tools(logging.getLogger("t"), None)
        names = [t.name for t in tools]
        assert set(names) == {"get_positions", "get_position_detail",
                              "get_pnl_summary", "get_trade_history"}

    def test_langchain_tools_callable(self, skill):
        tools = skill.build_langchain_tools()
        assert len(tools) == 4


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

class TestGetPositions:
    def test_normal(self, skill):
        data = _unwrap(skill.get_positions())
        assert len(data["positions"]) == 3
        hs = next(p for p in data["positions"] if p["stock_code"] == "513330")
        assert hs["is_etf"] is True
        assert hs["source"] == "eastmoney"
        assert data["meta"]["source_counts"].get("eastmoney") == 2
        assert data["meta"]["source_counts"].get("manual") == 1

    def test_source_filter(self, skill):
        data = _unwrap(skill.get_positions("eastmoney"))
        assert all(p["source"] == "eastmoney" for p in data["positions"])
        assert len(data["positions"]) == 2

    def test_account_null_when_unsynced(self, skill, tmp_path, monkeypatch):
        """东财未同步 → account 为 null，meta 提示（诚实标注）。"""
        # get_positions 内部对 server.services 的 import 异常全吞 → account=None
        import server.services.eastmoney_sync as em
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", None)
        db = _mk_db(tmp_path, db_file="test_unsynced.db")
        import tools.portfolio_data as pd
        data = _unwrap(pd.get_positions(db_path=db))
        assert data["account"] is None
        assert "未同步" in data["meta"]["note"]


# ---------------------------------------------------------------------------
# get_position_detail
# ---------------------------------------------------------------------------

class TestGetPositionDetail:
    def test_held(self, skill):
        data = _unwrap(skill.get_position_detail("002714"))
        assert data["held"] is True
        assert data["qty"] == 100
        assert data["avg_cost"] == pytest.approx(41.132)
        assert data["is_etf"] is False
        assert data["avail_qty"] == 100

    def test_etf_marked(self, skill):
        data = _unwrap(skill.get_position_detail("513330"))
        assert data["held"] is True
        assert data["is_etf"] is True

    def test_not_held(self, skill):
        """未持仓返回 held:false（让 agent 知道"没这只"）。"""
        data = _unwrap(skill.get_position_detail("300750"))
        assert data["held"] is False

    def test_bare_code_required(self, skill):
        result = skill.get_position_detail("sh600519")
        assert "无效" in result or "失败" in result


# ---------------------------------------------------------------------------
# get_pnl_summary
# ---------------------------------------------------------------------------

class TestGetPnlSummary:
    def test_summary(self, skill):
        data = _unwrap(skill.get_pnl_summary())
        assert data["position_count"] == 3
        # 总成本 = 100×41.132 + 52500×0.409 + 10×1620 = 4113.2+21472.5+16200
        assert data["total_cost"] == pytest.approx(41785.7, rel=1e-3)
        assert data["etf_count"] == 1
        assert data["etf_cost_ratio"] == pytest.approx(
            21472.5 / 41785.7, rel=1e-3)
        assert data["pnl_top3"][0]["code"] == "513330"  # 成本市值最大
        assert data["source_counts"].get("eastmoney") == 2

    def test_note_mentions_em_pnl(self, skill):
        data = _unwrap(skill.get_pnl_summary())
        assert "东财" in data["note"]

    def test_summary_account_fields(self, tmp_path, monkeypatch):
        """东财同步账户资金 → 汇总带总资产/可用/今日盈亏/现金比例。"""
        import server.services.eastmoney_sync as em
        db = _mk_db(tmp_path, db_file="test_acct.db")
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {
            "total_asset": 100000.0, "avail_cash": 25000.0,
            "balance": 25000.0, "positions_market_value": 75000.0,
            "today_pnl": -120.5, "synced_at": "2026-09-09T09:30:00",
        })
        import tools.portfolio_data as pd
        summary = pd.get_pnl_summary(db_path=db)
        assert summary["total_asset"] == pytest.approx(100000.0)
        assert summary["avail_cash"] == pytest.approx(25000.0)
        assert summary["today_pnl"] == pytest.approx(-120.5)
        assert summary["cash_ratio"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# get_trade_history
# ---------------------------------------------------------------------------

class TestGetTradeHistory:
    def test_empty_honest(self, skill):
        """空流水 → items 空且 meta.note 诚实标注（勿编造历史交易）。"""
        data = _unwrap(skill.get_trade_history())
        assert data["items"] == []
        assert "为空" in data["meta"]["note"]
        assert "勿编造" in data["meta"]["note"]

    def test_with_trades(self, tmp_path):
        db = _mk_db(tmp_path, with_trades=True)
        s = PortfolioQuerySkill(logging.getLogger("t"), None)
        import tools.portfolio_data as pd
        result = _unwrap(pd.get_trade_history(db_path=db))
        assert len(result["items"]) == 1
        assert result["items"][0]["stock_code"] == "600519"
        assert result["meta"]["note"] is None

    def test_code_filter(self, tmp_path):
        db = _mk_db(tmp_path, with_trades=True)
        import tools.portfolio_data as pd
        result = _unwrap(pd.get_trade_history(code="002714", db_path=db))
        assert result["items"] == []
        assert "为空" in result["meta"]["note"]

    def test_limit_clamped(self, skill):
        data = _unwrap(skill.get_trade_history(limit=5000))
        assert data["items"] == []  # clamp 不崩
