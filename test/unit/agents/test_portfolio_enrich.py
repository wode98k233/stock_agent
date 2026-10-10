"""持仓账本 agent 注入测试 — 门控 + 静默跳过 + 注入文本。

设计约束（设计文档 §3.5/§4.2）：
- 关键词门控（避免污染无关问题上下文）
- 无持仓静默跳过
- 注入异常不影响主流程（三层防护）
- 独立开关 PORTFOLIO_ENRICH_ENABLED
"""
import sqlite3

import pytest


@pytest.fixture
def db_with_position(tmp_path):
    """建临时 stock_radar.db 并写入一条持仓快照。"""
    db = tmp_path / "radar.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE portfolio_position (
            stock_code TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'cn',
            stock_name TEXT, qty REAL NOT NULL DEFAULT 0,
            avg_cost REAL NOT NULL DEFAULT 0, realized_pnl REAL DEFAULT 0,
            opened_at TEXT, updated_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual', avail_qty REAL,
            etf_flag INTEGER NOT NULL DEFAULT 0,
            UNIQUE(stock_code, market)
        );
        INSERT INTO portfolio_position
            (stock_code, market, stock_name, qty, avg_cost, realized_pnl, opened_at, updated_at)
        VALUES
            ('600519', 'cn', '贵州茅台', 100, 1300.0, 0, '2026-09-01', '2026-09-01'),
            ('000938', 'cn', '紫光股份', 0, 35.0, 500, '2026-08-01', '2026-09-02');
    """)
    conn.commit()
    conn.close()
    return str(db)


def _build(user_input, db_path, enabled=True):
    from unittest.mock import patch
    from config import Config
    from agents.base import _build_portfolio_block
    with patch.object(Config, "PORTFOLIO_ENRICH_ENABLED", enabled):
        return _build_portfolio_block(user_input, db_path=db_path)


class TestGating:
    def test_keyword_hit_with_positions(self, db_with_position):
        text = _build("我的持仓该清仓吗", db_with_position)
        assert "[用户持仓账本]" in text
        assert "贵州茅台(600519)" in text
        assert "1300.00" in text and "100" in text
        # qty=0 的清仓行不注入
        assert "紫光股份" not in text

    def test_keyword_variants_all_hit(self, db_with_position):
        for kw in ["持仓", "止损", "补仓", "仓位", "浮亏", "回本", "套牢", "解套", "我买了", "减仓"]:
            assert _build(f"帮我看看{kw}建议", db_with_position) != "", kw

    def test_no_keyword_skips_even_with_positions(self, db_with_position):
        """无关问题不注入（避免上下文污染）。"""
        assert _build("分析一下白酒板块", db_with_position) == ""
        assert _build("贵州茅台估值如何", db_with_position) == ""

    def test_no_positions_silent_skip(self, tmp_path):
        """无持仓（空库）静默跳过。"""
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE portfolio_position (
                stock_code TEXT, market TEXT, stock_name TEXT, qty REAL,
                avg_cost REAL, realized_pnl REAL, opened_at TEXT, updated_at TEXT,
                source TEXT DEFAULT 'manual', avail_qty REAL, etf_flag INTEGER DEFAULT 0);
        """)
        conn.commit()
        conn.close()
        assert _build("我该清仓吗", str(db)) == ""

    def test_disabled_by_switch(self, db_with_position):
        """PORTFOLIO_ENRICH_ENABLED=false → 不注入。"""
        assert _build("我该清仓吗", db_with_position, enabled=False) == ""

    def test_db_error_silent(self, tmp_path):
        """DB 文件不存在/异常 → 静默空串，不抛（不影响主流程）。"""
        assert _build("我该清仓吗", str(tmp_path / "nope.db")) == ""


class TestEnrichIntegration:
    def test_enrich_user_input_appends_portfolio(self, db_with_position, monkeypatch):
        """_enrich_user_input 集成：命中关键词时增强输入含持仓块。"""
        from agents.base import _build_portfolio_block
        from unittest.mock import patch
        import agents.base as base_mod

        captured = {}

        def fake_build(user_input, db_path=None):
            captured["called"] = True
            return "[用户持仓账本]\n贵州茅台(600519) 成本1300.00×100股"

        monkeypatch.setattr(base_mod, "_build_portfolio_block", fake_build)
        agent = base_mod.BaseAgent.__new__(base_mod.BaseAgent)  # 跳过 __init__
        result = agent._enrich_user_input("我该清仓吗")

        assert captured["called"] is True
        assert "[用户持仓账本]" in result

    def test_build_block_exception_swallowed(self, monkeypatch):
        """注入函数抛异常 → _enrich_user_input 不受影响（三层防护）。"""
        import agents.base as base_mod

        def boom(user_input, db_path=None):
            raise RuntimeError("injection boom")

        monkeypatch.setattr(base_mod, "_build_portfolio_block", boom)
        agent = base_mod.BaseAgent.__new__(base_mod.BaseAgent)
        result = agent._enrich_user_input("我该清仓吗")
        assert "我该清仓吗" in result  # 原始输入仍在


class TestAccountInjected:
    def test_account_line_when_cache_exists(self, db_with_position):
        """东财账户资金存在 → 注入账户总资产/可用资金/今日盈亏行。"""
        from unittest.mock import patch
        with patch("server.services.eastmoney_sync.get_account_cache",
                   return_value={"total_asset": 100000.0, "avail_cash": 35000.0,
                                 "balance": 35000.0,
                                 "positions_market_value": 65000.0,
                                 "today_pnl": 123.45,
                                 "synced_at": "2026-09-09"}):
            text = _build("我的持仓该清仓吗", db_with_position)
        assert "账户：" in text
        assert "账户总资产约100,000.00元" in text
        assert "可用资金约35,000.00元" in text
        assert "今日盈亏+123.45元" in text

    def test_no_account_line_when_cache_missing(self, db_with_position,
                                                monkeypatch):
        """账号资金缺失 → 不编造账户行，仅保留持仓明细。"""
        import server.services.eastmoney_sync as em
        monkeypatch.setattr(em, "_ACCOUNT_CACHE", {})
        text = _build("我的持仓该清仓吗", db_with_position)
        assert "账户：" not in text
        assert "贵州茅台(600519)" in text

    def test_etf_ratio_line(self, tmp_path):
        """含 ETF 持仓 → 注入 ETF 占组合成本比例。"""
        db = str(tmp_path / "etf.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE portfolio_position (
                stock_code TEXT NOT NULL, market TEXT NOT NULL DEFAULT 'cn',
                stock_name TEXT, qty REAL NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0, realized_pnl REAL DEFAULT 0,
                opened_at TEXT, updated_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual', avail_qty REAL,
                etf_flag INTEGER NOT NULL DEFAULT 0,
                UNIQUE(stock_code, market)
            );
        """)
        conn.executemany(
            "INSERT INTO portfolio_position(stock_code, market, stock_name,"
            " qty, avg_cost, realized_pnl, opened_at, updated_at, source,"
            " avail_qty, etf_flag) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("510300", "cn", "沪深300ETF", 10000, 0.5, 0,
                 "2026-09-01", "2026-09-01", "eastmoney", 10000, 1),
                ("600519", "cn", "贵州茅台", 100, 1300.0, 0,
                 "2026-09-01", "2026-09-01", "eastmoney", 100, 0),
            ])
        conn.commit()
        conn.close()
        text = _build("我该减仓吗", db)
        assert "ETF 占组合成本约" in text
        assert "3.7%" in text  # 5000 / (5000+130000) ≈ 3.70%
