"""scripts/ops/audit_market_coverage.py 的单元测试。

全部使用临时 SQLite，不依赖真实 db/market_data.db，不联网。
覆盖：分桶边界、窗口过滤、字段完整率、单只诊断、只读连接、文本渲染。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ops" / "audit_market_coverage.py"
_spec = importlib.util.spec_from_file_location("audit_market_coverage", SCRIPT)
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)


STOCK_DAILY_DDL = """
CREATE TABLE stock_daily (
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    pre_close REAL,
    pct_change REAL,
    change_amount REAL,
    volume REAL,
    amount REAL,
    turnover_rate REAL,
    amplitude REAL,
    trade_status TEXT,
    is_st INTEGER,
    source TEXT,
    updated_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (code, trade_date)
)
"""

COLUMNS = (
    "code", "trade_date", "open", "high", "low", "close", "pre_close",
    "pct_change", "change_amount", "volume", "amount", "turnover_rate",
    "amplitude", "trade_status", "is_st", "source",
)


def _row(code, day, close, source="akshare", **kwargs):
    values = {
        "code": code, "trade_date": day, "open": None, "high": None, "low": None,
        "close": close, "pre_close": None, "pct_change": None,
        "change_amount": None, "volume": None, "amount": None,
        "turnover_rate": None, "amplitude": None, "trade_status": "active",
        "is_st": 0, "source": source,
    }
    values.update(kwargs)
    return tuple(values[col] for col in COLUMNS)


@pytest.fixture()
def tmp_db(tmp_path):
    """构造 4 只标的小样本：5 / 3 / 1 / 31 根，含 null 与周末行。"""
    db_path = tmp_path / "market.db"
    conn = sqlite3.connect(db_path)
    conn.execute(STOCK_DAILY_DDL)

    rows = [
        # 000001：2 根 close-only 合成行（含周末）+ 3 根完整行
        _row("000001", "2024-01-06", 10.0),
        _row("000001", "2024-01-07", 11.0),
        _row("000001", "2024-12-30", 12.0, open=11.5, high=12.0, low=11.4,
             volume=100.0, amount=1200.0, pre_close=11.5, pct_change=4.35,
             change_amount=0.5, turnover_rate=1.1, amplitude=4.0),
        _row("000001", "2025-01-02", 13.0, source="hithink_dump", open=12.5,
             high=13.0, low=12.4, volume=110.0, amount=1300.0, pre_close=12.0,
             pct_change=8.33, change_amount=1.0, turnover_rate=1.2, amplitude=2.2),
        _row("000001", "2025-01-03", 14.0, source="hithink_dump", open=13.5,
             high=14.0, low=13.4, volume=120.0, amount=1400.0, pre_close=13.0,
             pct_change=7.69, change_amount=1.0, turnover_rate=1.3, amplitude=2.3),
        # 600519：3 根（其中 1 根在 2026，用于窗口过滤）
        _row("600519", "2025-01-02", 100.0, source="hithink", open=99.0,
             high=101.0, low=98.5, volume=200.0, amount=20000.0,
             pre_close=99.0, pct_change=1.01, change_amount=1.0),
        _row("600519", "2025-01-03", 101.0, source="hithink", open=100.0,
             high=102.0, low=99.5, volume=210.0, amount=21000.0,
             pre_close=100.0, pct_change=1.00, change_amount=1.0),
        _row("600519", "2026-01-05", 102.0, source="hithink", open=101.0,
             high=103.0, low=100.5, volume=220.0, amount=22000.0,
             pre_close=101.0, pct_change=0.99, change_amount=1.0),
        # 510300：1 根 close-only
        _row("510300", "2025-06-02", 4.0, source="akshare_etf"),
    ]
    # 000002：31 根（每周一根，落在 31~120 桶）
    day = date(2025, 1, 1)
    for _ in range(31):
        rows.append(
            _row("000002", day.isoformat(), 20.0, open=19.5, high=20.5, low=19.0,
                 volume=300.0, amount=6000.0, pre_close=19.5, pct_change=2.56)
        )
        day += timedelta(days=7)

    conn.executemany(
        f"INSERT INTO stock_daily ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in COLUMNS)})",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bars,label",
    [
        (1, "1~30"), (30, "1~30"),
        (31, "31~120"), (120, "31~120"),
        (121, "121~250"), (250, "121~250"),
        (251, "251~399"), (399, "251~399"),
        (400, "≥400"), (10_000, "≥400"),
    ],
)
def test_bucket_of_boundaries(bars, label):
    assert audit.bucket_of(bars) == label


@pytest.mark.parametrize("bars", [0, -1, -400])
def test_bucket_of_rejects_non_positive(bars):
    with pytest.raises(ValueError):
        audit.bucket_of(bars)


def test_bucket_stats_counts_and_pct():
    stats = audit.bucket_stats([1, 30, 31, 120, 121, 250, 251, 399, 400, 800])
    assert [row["codes"] for row in stats] == [2, 2, 2, 2, 2]
    assert all(row["pct"] == 20.0 for row in stats)
    assert [row["label"] for row in stats] == ["1~30", "31~120", "121~250", "251~399", "≥400"]


def test_completeness_rates_partial_and_zero_denominator():
    rates = audit.completeness_rates({"pre_close": 2, "pct_change": 0}, total_rows=4)
    assert rates["pre_close"] == 0.5
    assert rates["pct_change"] == 0.0
    assert rates["amount"] == 0.0
    assert audit.completeness_rates({"pre_close": 0}, total_rows=0)["pre_close"] is None


def test_parse_codes_and_is_weekend():
    assert audit.parse_codes("600519, 000001;600519") == ["600519", "000001"]
    assert audit.parse_codes("") == []
    assert audit.is_weekend("2024-01-06") is True
    assert audit.is_weekend("2024-01-08") is False


# ---------------------------------------------------------------------------
# IO：窗口过滤 / 完整率 / 单只诊断
# ---------------------------------------------------------------------------
def test_connect_is_read_only(tmp_db):
    conn = audit.connect_readonly(tmp_db)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_fail (x INTEGER)")
    finally:
        conn.close()


def test_load_bar_counts_window_filter(tmp_db):
    conn = audit.connect_readonly(tmp_db)
    try:
        full = audit.load_bar_counts(conn)
        assert full == {"000001": 5, "000002": 31, "510300": 1, "600519": 3}

        window = audit.load_bar_counts(conn, "2025-01-01", "2025-12-31")
        assert window == {"000001": 2, "000002": 31, "510300": 1, "600519": 2}
    finally:
        conn.close()


def test_load_completeness_all_and_window(tmp_db):
    conn = audit.connect_readonly(tmp_db)
    try:
        full = audit.load_completeness(conn)
        assert full["rows"] == 40
        assert full["non_null"]["pre_close"] == 37
        assert full["non_null"]["change_amount"] == 6
        assert full["non_null"]["amount"] == 37
        assert full["non_null"]["turnover_rate"] == 3
        assert full["rates"]["pre_close"] == pytest.approx(37 / 40)

        window = audit.load_completeness(conn, "2025-01-01", "2025-12-31")
        assert window["rows"] == 36
        assert window["non_null"]["pre_close"] == 35
        assert window["non_null"]["change_amount"] == 4
        assert window["non_null"]["turnover_rate"] == 2
    finally:
        conn.close()


def test_load_diagnostic_detects_close_only_and_weekend(tmp_db):
    conn = audit.connect_readonly(tmp_db)
    try:
        diag = audit.load_diagnostic(conn, "000001", "2025-01-01", "2025-12-31")
        assert diag["rows"] == 5
        assert diag["window_rows"] == 2
        assert diag["first_date"] == "2024-01-06"
        assert diag["last_date"] == "2025-01-03"
        assert diag["weekend_rows"] == 2
        assert diag["close_only_rows"] == 2
        assert diag["close_null_rows"] == 0
        assert diag["non_null"]["pre_close"] == 3
        assert diag["sources"] == [{"source": "akshare", "rows": 3}, {"source": "hithink_dump", "rows": 2}]

        missing = audit.load_diagnostic(conn, "999999", "2025-01-01", "2025-12-31")
        assert missing["rows"] == 0
        assert missing["window_rows"] == 0
        assert missing["sources"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 组装 + 渲染 + JSON
# ---------------------------------------------------------------------------
def test_build_audit_full_report(tmp_db):
    conn = audit.connect_readonly(tmp_db)
    try:
        result = audit.build_audit(
            conn,
            start="2025-01-01",
            end="2025-12-31",
            top_n=2,
            codes=["000001"],
            db_path=tmp_db,
            generated_at="2026-10-20 00:00:00",
        )
    finally:
        conn.close()

    assert result["read_only"] is True
    assert result["overview"] == {"rows": 40, "codes": 4, "avg_bars": 10.0}
    assert result["window"]["rows"] == 36
    assert result["window"]["codes"] == 4
    assert result["window"]["ge_qualified_count"] == 0
    assert [row["codes"] for row in result["buckets"]] == [3, 1, 0, 0, 0]
    assert result["top"]["fewest"] == [
        {"code": "510300", "bars": 1},
        {"code": "600519", "bars": 3},
    ]
    assert result["top"]["most"] == [{"code": "000002", "bars": 31}, {"code": "000001", "bars": 5}]
    assert [d["code"] for d in result["diagnostics"]] == ["000001"]
    assert result["hygiene"]["weekend_rows"] == 2
    assert result["hygiene"]["weekend_codes"] == 1
    assert result["hygiene"]["non_standard_codes"] == 0

    text = audit.render_text(result)
    assert "只读" in text
    assert "不写库、不联网、不补数" in text
    assert "1~30" in text and "≥400" in text
    assert "pre_close" in text
    assert "000001" in text and "close-only" in text

    payload = json.dumps(result, ensure_ascii=False)
    assert json.loads(payload)["window"]["ge_qualified_count"] == 0
    assert json.loads(payload)["read_only"] is True


def test_missing_db_is_not_created(tmp_path):
    """只读连接不会顺手建库（mode=ro）。"""
    missing = tmp_path / "missing.db"
    assert not missing.exists()
    with pytest.raises(sqlite3.OperationalError):
        audit.connect_readonly(missing)
    assert not missing.exists()
