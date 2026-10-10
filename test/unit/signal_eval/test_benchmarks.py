"""P6 行业基准映射测试 — 板块名关键词 → 行业指数，个股映射与兜底。

设计（signal-eval-phase2 §8 第一步）：
- 个股 → 东财行业板块（stock_board_member × stock_board）
- 板块名关键词 → index_daily 已有行业指数（消费/白酒/医药/新能源…）
- 无映射或指数无 K 线 → 000300 兜底
- 映射构建失败 fail-safe（不阻断 alpha 计算）
"""
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def bench_db(tmp_path):
    """临时 market_data.db：board/member 两表 + 模拟板块归属。"""
    db = tmp_path / "market_data.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE stock_board (
            board_id TEXT PRIMARY KEY, board_code TEXT, board_name TEXT,
            board_type TEXT, provider TEXT, parent_board_id TEXT,
            is_active INTEGER, updated_at TEXT, raw_json TEXT);
        CREATE TABLE stock_board_member (
            board_id TEXT, code TEXT, as_of_date TEXT, is_primary INTEGER,
            weight REAL, rank INTEGER, reason TEXT, source TEXT,
            updated_at TEXT, raw_json TEXT);
        INSERT INTO stock_board VALUES
            ('industry_BK0999', 'BK0999', '酿酒行业', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1000', 'BK1000', '食品饮料', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1001', 'BK1001', '化学制药', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1002', 'BK1002', '光伏设备', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1003', 'BK1003', '汽车整车', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1004', 'BK1004', '半导体', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1005', 'BK1005', '银行', 'industry', 'akshare', NULL, 1, '', NULL),
            ('industry_BK1006', 'BK1006', '半导体设备', 'industry', 'akshare', NULL, 1, '', NULL),
            ('concept_BK9000', 'BK9000', 'AI算力', 'concept', 'akshare', NULL, 1, '', NULL);
        INSERT INTO stock_board_member VALUES
            ('industry_BK0999', '600519', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1000', '000858', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1001', '600276', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1002', '601012', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1003', '600104', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1004', '688981', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1004', '300001', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('industry_BK1006', '300001', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL),
            ('concept_BK9000', '300002', '2026-09-01', 1, NULL, NULL, NULL, 't', '', NULL);
    """)
    conn.commit()
    conn.close()
    return str(db)


class TestKeywordTable:
    def test_covers_all_industry_indices_in_index_daily(self):
        """关键词表须覆盖 index_daily 已有行业/主题指数（宽基除外）。"""
        from signal_eval.benchmarks import INDUSTRY_INDEX_KEYWORDS
        codes = {code for code, _label, _kw in INDUSTRY_INDEX_KEYWORDS}
        for expected in ("000932", "399997", "000991", "399441", "399808", "399976"):
            assert expected in codes, f"关键词表缺行业指数 {expected}"

    def test_keyword_ordered_specific_first(self):
        """专一指数在前（白酒先于消费），避免泛关键词吞掉精确匹配。"""
        from signal_eval.benchmarks import INDUSTRY_INDEX_KEYWORDS
        order = [code for code, _label, _kw in INDUSTRY_INDEX_KEYWORDS]
        assert order.index("399997") < order.index("000932")  # 白酒 → 消费


class TestBuildMapping:
    def test_build_from_board_member(self, bench_db):
        from signal_eval.benchmarks import build_bench_mapping
        mapping = build_bench_mapping(db_path=bench_db)
        assert mapping["600519"] == "399997"   # 酿酒行业 → 中证白酒
        assert mapping["600276"] == "000991"   # 化学制药 → 全指医药
        assert mapping["601012"] == "399808"   # 光伏设备 → 中证新能源
        assert mapping["600104"] == "399976"   # 汽车整车 → 新能源车

    def test_unmatched_board_excluded(self, bench_db):
        """无对应行业指数的板块（半导体/银行）不进映射 → 调用方兜底。"""
        from signal_eval.benchmarks import build_bench_mapping
        mapping = build_bench_mapping(db_path=bench_db)
        assert "688981" not in mapping  # 半导体（无行业指数）

    def test_db_missing_fail_safe(self, tmp_path):
        """DB 不存在 → 空映射（调用方全兜底 000300），不抛。"""
        from signal_eval.benchmarks import build_bench_mapping
        assert build_bench_mapping(db_path=str(tmp_path / "nope.db")) == {}

    def test_member_cache_reused_across_calls(self, bench_db):
        """模块级缓存：同库二次构建不重读 DB（连接计数验证或直接断言缓存命中）。"""
        import signal_eval.benchmarks as bm
        with patch.object(bm, "_load_mapping_from_db") as spy:
            bm.build_bench_mapping(db_path=bench_db)
            bm.build_bench_mapping(db_path=bench_db)
        assert spy.call_count == 1  # 第二次走缓存


class TestResolveBenchmark:
    def test_hit_industry_index(self, bench_db):
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("600519") == "399997"
            assert resolve_benchmark("600276") == "000991"

    def test_miss_falls_back_to_csi300(self, bench_db):
        """无映射股票（未覆盖/银行/ETF）→ 000300 兜底。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("600015") == "000300"   # 银行板块无指数
            assert resolve_benchmark("999999") == "000300"   # 无归属股票

    def test_db_error_falls_back(self, tmp_path):
        """映射构建异常 → 兜底 000300（alpha 计算永不因映射挂掉）。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", str(tmp_path / "no.db")):
            assert resolve_benchmark("600519") == "000300"

    def test_bare_code_input(self, bench_db):
        """输入 'sh600519' 带前缀 → 剥前缀后命中。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("sh600519") == "399997"


def _seed_board_kline(db_path: str, codes, end: str = "2026-09-05") -> None:
    """给临时库补 stock_daily 板块K线（模拟已采集板块）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_daily (
        code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL,
        low REAL, close REAL, pre_close REAL, pct_change REAL, change_amount REAL,
        volume REAL, amount REAL, turnover_rate REAL, amplitude REAL,
        trade_status TEXT, is_st INTEGER, source TEXT, updated_at TEXT, raw_json TEXT,
        PRIMARY KEY (code, trade_date))""")
    for c in codes:
        conn.execute("INSERT INTO stock_daily (code, trade_date, close) VALUES (?, ?, 100.0)", (c, end))
    conn.commit()
    conn.close()


class TestBoardKlineBench:
    """P6 第二步：板块K线作为行业基准的中接层（关键词指数优先级不变）。"""

    def test_board_kline_used_when_no_keyword_match(self, bench_db):
        """半导体板块无行业指数关键词，但有板块K线 → 用板块K线代码。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        _seed_board_kline(bench_db, ["board_industry_半导体"])
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("688981") == "board_industry_半导体"

    def test_keyword_index_wins_over_board_kline(self, bench_db):
        """关键词指数优先（口径不变）：酿酒行业即使有板块K线仍用 399997。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        _seed_board_kline(bench_db, ["board_industry_酿酒行业"])
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("600519") == "399997"

    def test_specific_board_preferred_when_multiple(self, bench_db):
        """多板块归属：成分数最少的板块最专一，优先（半导体设备 1 只 < 半导体 2 只）。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        _seed_board_kline(bench_db, ["board_industry_半导体", "board_industry_半导体设备"])
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("300001") == "board_industry_半导体设备"

    def test_falls_to_larger_board_when_specific_no_kline(self, bench_db):
        """最专一板块无K线 → 落到次专一有K线的板块。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        _seed_board_kline(bench_db, ["board_industry_半导体"])
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("300001") == "board_industry_半导体"

    def test_no_kline_no_keyword_still_unmapped(self, bench_db):
        """无关键词指数、无板块K线 → 不进映射 → 兜底 000300。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("688981") == "000300"

    def test_concept_board_never_used_as_bench(self, bench_db):
        """概念板块不作为行业基准（即使有板块K线）。"""
        from signal_eval.benchmarks import resolve_benchmark, reset_cache
        _seed_board_kline(bench_db, ["board_concept_AI算力"])
        reset_cache()
        with patch("signal_eval.benchmarks._MAPPING_DB_PATH", bench_db):
            assert resolve_benchmark("300002") == "000300"
