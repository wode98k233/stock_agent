"""标的解析单测：数据来自临时 SQLite（离线、可复现），**不允许代码里写别名表**。

红线守卫已升级为**全库扫描**：`test/unit/test_no_hardcoded_symbol_aliases.py`
（旧版只扫本模块一个文件，属假达标——另外三处同名表它都没看见）。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agents.deep_report import resolver as R


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "market_data.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE index_info (code TEXT, name TEXT);
        CREATE TABLE stock_board (board_code TEXT, board_name TEXT, board_type TEXT);
        CREATE TABLE stock_info (code TEXT, name TEXT);
        INSERT INTO index_info VALUES ('399986', '中证银行指数'), ('000300', '沪深300'),
                                      ('000134', '上证银行'), ('000951', '300银行');
        INSERT INTO stock_board VALUES ('BK1283', '银行', 'industry'), ('gn_0001', '人工智能', 'concept');
        INSERT INTO stock_info VALUES ('sh600036', '招商银行'), ('sz000001', '平安银行');
    """)
    conn.commit()
    conn.close()
    return str(path)


class TestCode:
    def test_detects_code_forms(self):
        assert R.looks_like_code("399986.SZ") and R.looks_like_code("600036")
        assert not R.looks_like_code("银行板块")

    def test_normalize(self):
        assert R.normalize_code("600036") == "600036.SH"
        assert R.normalize_code("000001") == "000001.SZ"
        assert R.normalize_code("830799") == "830799.BJ"
        assert R.normalize_code("sh600036") == "600036.SH"
        assert R.normalize_code("BK1283") == "BK1283"


class TestDbLookup:
    def test_index_exact(self, db):
        assert R.lookup_db("中证银行指数", db) == "399986.SZ"

    def test_candidates_surface_ambiguity(self, db):
        """「银行」有多个候选时，不硬猜唯一解：指数候选优先，其余按匹配度。"""
        cands = R.search("银行板块", db)
        codes = [c["code"] for c in cands]
        assert len(codes) >= 2
        assert "399986.SZ" in codes and "000134.SH" in codes
        # 指数优先于板块（BK1283 是板块码，排在指数后面）
        assert codes.index("000134.SH") < codes.index("BK1283")

    def test_board_lookup_when_no_index_match(self, db):
        assert R.lookup_db("人工智能", db) == "gn_0001"

    def test_index_suffix_rules(self):
        assert R.normalize_index_code("000300") == "000300.SH"
        assert R.normalize_index_code("399986") == "399986.SZ"
        assert R.normalize_index_code("899050") == "899050.BJ"

    def test_lookup_db_index_suffix(self, db):
        assert R.lookup_db("沪深300", db) == "000300.SH"

    def test_stock_lookup(self, db):
        assert R.lookup_db("招商银行", db) == "600036.SH"

    def test_unknown_returns_empty(self, db):
        assert R.lookup_db("不存在的板块", db) == ""

    def test_missing_db_returns_empty(self, tmp_path):
        assert R.lookup_db("银行板块", str(tmp_path / "nope.db")) == ""


class TestResolve:
    def test_code_passthrough(self, db):
        assert R.resolve("399986.SZ", db_path=db) == "399986.SZ"

    def test_name_with_code(self, db):
        assert R.resolve("中证银行(399986.SZ)", db_path=db) == "399986.SZ"

    def test_db_first(self, db):
        # 指数优先：词面最接近的指数是「上证银行」（后缀命中）
        assert R.resolve("银行板块", db_path=db) == "000134.SH"

    def test_query_fn_only_as_fallback(self, db):
        payload = json.dumps({"data": {"datas": [{"指数代码": "399986.SZ"}]}}, ensure_ascii=False)
        called = {"n": 0}

        def q(_):
            called["n"] += 1
            return payload

        # 库里能查到 → 不应调用 query_fn
        assert R.resolve("中证银行", db_path=db, query_fn=q) == "399986.SZ"
        assert called["n"] == 0
        # 库里查不到 → 才回落到问句
        assert R.resolve("某个新板块", db_path=db, query_fn=q) == "399986.SZ"
        assert called["n"] == 1
