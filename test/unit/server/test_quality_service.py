"""A7 质量观测聚合单测：fixture 临时 SQLite 四库（不碰真实数据，AC-6）。

覆盖：
- overview 正常聚合（北极星/环比/周趋势，AC-4 北京时间周边界）
- 缺库降级（None 连接不抛错、字段置空，AC-2）
- 矩阵聚合（mode×scenario、judge 分、成本/耗时、scenario 推断）
- n<5 → color='na'（AC-3）
- 空数据/缺库不 500（不抛异常）
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from server import quality_service as qs

_BJ = timezone(timedelta(hours=8))
# 固定「现在」：2026-09-09（周三，ISO W37）北京时间 10:00 —— 周边界判定不受当日钟点影响
NOW = datetime(2026, 9, 9, 10, 0, tzinfo=_BJ)


# ── fixture：临时最小 schema 四库 ──────────────────────────────────

def _create_db(path: str, ddl: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    for stmt in ddl:
        conn.execute(stmt)
    return conn


@pytest.fixture()
def dbs(tmp_path):
    """构建临时四库并返回 QualityDBs（只读重开，走与生产一致的 mode=ro 路径）。"""
    base = str(tmp_path)

    radar = _create_db(f"{base}/stock_radar.db", [
        """CREATE TABLE web_messages (
            id INTEGER PRIMARY KEY, message_uuid TEXT, dialog_uuid TEXT,
            role TEXT, content TEXT, mode TEXT, trace_run_id TEXT, created_at TEXT)""",
    ])
    trace = _create_db(f"{base}/agent_trace.db", [
        """CREATE TABLE runs (
            id TEXT PRIMARY KEY, agent_name TEXT, created_at TEXT, duration_ms REAL)""",
        """CREATE TABLE steps (
            id INTEGER PRIMARY KEY, run_id TEXT, step_type TEXT, step_name TEXT,
            output TEXT, extra TEXT)""",
    ])
    signal = _create_db(f"{base}/signal_eval.db", [
        """CREATE TABLE signal (
            id INTEGER PRIMARY KEY, dialog_uuid TEXT, message_uuid TEXT,
            symbol_key TEXT, decision TEXT, confidence REAL,
            is_market_wide INTEGER, signal_time TEXT)""",
    ])
    eval_conn = _create_db(f"{base}/eval.db", [
        """CREATE TABLE eval_cases (
            id INTEGER PRIMARY KEY, trace_run_id TEXT, overall_score REAL)""",
    ])

    for c in (radar, trace, signal, eval_conn):
        c.commit()
        c.close()

    # 以只读方式重开（与 open_quality_dbs 同口径）
    def _ro(name):
        return sqlite3.connect(f"file:{base}/{name}?mode=ro", uri=True)

    quality = qs.QualityDBs(signal=_ro("signal_eval.db"), radar=_ro("stock_radar.db"),
                            trace=_ro("agent_trace.db"), eval=_ro("eval.db"))
    yield quality, base
    qs.close_quality_dbs(quality)


def _iso_utc(dt_bj: datetime) -> str:
    return dt_bj.astimezone(timezone.utc).isoformat()


def _add_message(radar_conn, mid, dialog_uuid, run_id, mode, created_bj, content=""):
    radar_conn.execute(
        "INSERT INTO web_messages (id, dialog_uuid, role, content, mode, trace_run_id, created_at) "
        "VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
        (mid, dialog_uuid, content, mode, run_id, _iso_utc(created_bj)),
    )


def _add_run(trace_conn, run_id, agent_name, duration_ms, created_bj, tokens=None, template_id=None):
    trace_conn.execute(
        "INSERT INTO runs (id, agent_name, created_at, duration_ms) VALUES (?, ?, ?, ?)",
        (run_id, agent_name, _iso_utc(created_bj), duration_ms),
    )
    if tokens is not None:
        trace_conn.execute(
            "INSERT INTO steps (run_id, step_type, step_name, extra) VALUES (?, 'llm', 'chat', ?)",
            (run_id, json.dumps({"token_usage": {"input_tokens": tokens, "output_tokens": 0}})),
        )
    if template_id is not None:
        trace_conn.execute(
            "INSERT INTO steps (run_id, step_type, step_name, output) VALUES (?, 'tool', 'select_template', ?)",
            (run_id, json.dumps({"selected_template_id": template_id})),
        )


# ── 时间口径（AC-4） ───────────────────────────────────────────────

def test_week_key_beijing_boundary():
    """周一 00:00（北京时间）换周：UTC 周日 16:00 已属下一 ISO 周。"""
    # 北京时间 2024-01-01（周一）00:00 == UTC 2023-12-31 16:00 → W01（2024）
    assert qs._week_key_beijing("2023-12-31T16:00:00+00:00") == "W01"
    # 北京时间 2023-12-31（周日）23:59 == UTC 15:59 → 仍属 2023 W52
    assert qs._week_key_beijing("2023-12-31T15:59:00+00:00") == "W52"


def test_week_key_handles_garbage():
    assert qs._week_key_beijing("not-a-date") == "W00"
    assert qs._week_key_beijing(None) == "W00"


# ── overview ───────────────────────────────────────────────────────

def test_overview_normal_aggregation(dbs):
    quality, base = dbs
    radar = sqlite3.connect(f"{base}/stock_radar.db")
    signal = sqlite3.connect(f"{base}/signal_eval.db")
    # 本周（W37：周一 09-07 起）2 条 + 上周 1 条（环比 +100%）
    for i, dt in enumerate([NOW - timedelta(days=1), NOW - timedelta(days=2), NOW - timedelta(days=8)]):
        signal.execute(
            "INSERT INTO signal (dialog_uuid, decision, symbol_key, is_market_wide, signal_time) "
            "VALUES (?, 'buy', ?, 0, ?)", (f"d{i}", f"sh60051{i}", _iso_utc(dt)),
        )
    # 本周 1 条无 confidence、1 条有 confidence：当前口径降级为 tracked（两者都计）
    radar.close(); signal.commit(); signal.close()

    data = qs.build_overview(quality, now=NOW)
    assert data["week"] == "W37"
    assert data["verifiable_basis"] == "tracked"
    assert data["verifiable_conclusions"] == 2
    assert data["prev_week_count"] == 1
    assert data["wow_pct"] == 100
    assert len(data["trend"]) == 8
    assert data["trend"][-1]["week"] == "W37"
    assert data["trend"][-1]["count"] == 2
    assert data["trend"][-2]["count"] == 1
    # 胜率样本池 = buy 且已补标且非大盘 = 3（收益口径本期不接 → value null）
    assert data["win_rate_after_cost"]["value"] is None
    assert data["win_rate_after_cost"]["n"] == 3
    assert data["cache_hit_rate"] is None  # A8 未上线


def test_overview_missing_db_degrades(dbs):
    """signal 库连接为 None：北极星/趋势置空，不抛错（AC-2）。"""
    quality, base = dbs
    degraded = qs.QualityDBs(signal=None, radar=quality.radar,
                             trace=quality.trace, eval=quality.eval)
    try:
        data = qs.build_overview(degraded, now=NOW)
        assert data["verifiable_conclusions"] is None
        assert data["trend"] == []
        assert data["wow_pct"] is None
    finally:
        qs.close_quality_dbs(degraded)


def test_overview_all_dbs_none():
    """四库全缺：返回骨架 dict，不抛异常（AC-2/空数据不 500）。"""
    empty = qs.QualityDBs(signal=None, radar=None, trace=None, eval=None)
    data = qs.build_overview(empty, now=NOW)
    assert data["verifiable_conclusions"] is None
    assert data["win_rate_after_cost"]["value"] is None
    assert data["win_rate_after_cost"]["n"] == 0
    assert data["avg_cost_per_report"]["value"] is None
    assert data["avg_cost_per_report"]["n"] == 0


def test_overview_confidence_basis_upgrade(dbs):
    """A2 上线后（confidence 非空存在）：口径自动切 with_confidence。"""
    quality, base = dbs
    signal = sqlite3.connect(f"{base}/signal_eval.db")
    signal.execute(
        "INSERT INTO signal (decision, confidence, signal_time) VALUES ('buy', 0.8, ?)",
        (_iso_utc(NOW - timedelta(days=1)),),
    )
    signal.execute(
        "INSERT INTO signal (decision, confidence, signal_time) VALUES ('buy', NULL, ?)",
        (_iso_utc(NOW - timedelta(days=1)),),
    )
    signal.commit(); signal.close()
    data = qs.build_overview(quality, now=NOW)
    assert data["verifiable_basis"] == "with_confidence"
    assert data["verifiable_conclusions"] == 1  # 仅 confidence 非空计入


# ── matrix ─────────────────────────────────────────────────────────

_DASH_CONTENT = '前言 @@DASHBOARD_START@@{"index_data":{"sh":1},"dashboard_id":"market"}@@DASHBOARD_END@@'


def test_matrix_normal_aggregation(dbs):
    quality, base = dbs
    radar = sqlite3.connect(f"{base}/stock_radar.db")
    trace = sqlite3.connect(f"{base}/agent_trace.db")
    eval_conn = sqlite3.connect(f"{base}/eval.db")
    signal = sqlite3.connect(f"{base}/signal_eval.db")

    # 6 条 unified_plan × 大盘消息（n=6 ≥5 → 参与色阶）
    for i in range(6):
        run_id = f"run-{i}"
        _add_message(radar, i + 1, f"dialog-{i}", run_id, "unified_plan",
                     NOW - timedelta(days=i + 1), content=_DASH_CONTENT)
        _add_run(trace, run_id, "unified_plan", duration_ms=60_000 + i * 1000,
                 created_bj=NOW - timedelta(days=i + 1), tokens=1000 + i)
    # 2 条 react_stock（dashboard_id 缺失 → select_template 模板推断 → 个股）
    for i in range(2):
        run_id = f"react-{i}"
        _add_message(radar, 10 + i, f"rdialog-{i}", run_id, None,
                     NOW - timedelta(days=i + 1), content="纯文本无 dashboard")
        _add_run(trace, run_id, "react_stock", duration_ms=30_000,
                 created_bj=NOW - timedelta(days=i + 1), template_id="valuation_percentile")
    # LLM 裁判分挂在 run-0
    eval_conn.execute("INSERT INTO eval_cases (trace_run_id, overall_score) VALUES ('run-0', 8.0)")
    # 1 条 signal 归属 dialog-1（buy，可归因）
    signal.execute(
        "INSERT INTO signal (dialog_uuid, decision, symbol_key, is_market_wide, signal_time) "
        "VALUES ('dialog-1', 'buy', 'sh600519', 0, ?)", (_iso_utc(NOW - timedelta(days=2)),),
    )
    for c in (radar, trace, eval_conn, signal):
        c.commit(); c.close()

    data = qs.build_matrix(quality, days=90, now=NOW)
    assert data["dims"] == ["mode", "scenario"]

    up_cell = next(c for c in data["cells"] if c["mode"] == "unified_plan")
    assert up_cell["scenario"] == "大盘"          # dashboard_id='market' 推断
    assert up_cell["n"] == 6
    assert up_cell["judge_avg"] == pytest.approx(8.0)   # eval_cases.trace_run_id 关联
    assert up_cell["judge_n"] == 1
    assert up_cell["avg_cost"] == 1002            # (1000+..+1005)/6，unit=tokens
    assert up_cell["avg_latency_s"] == pytest.approx(62.5, abs=0.1)  # runs.duration_ms 均值
    assert up_cell["win_rate"] is None            # 收益口径本期不接
    assert up_cell["win_n"] == 1                  # dialog-1 的 buy 信号归到本格
    assert up_cell["color"] != "na"               # n≥5 且 judge 有值 → 有色阶

    react_cell = next(c for c in data["cells"] if c["mode"] == "react_stock")
    assert react_cell["scenario"] == "个股"        # select_template → valuation_percentile
    assert react_cell["color"] == "na"            # n=2 <5 → 灰底无色阶（AC-3）

    # 覆盖率：场景识别 = 8/8；胜率归因 1/1；评估 1/8
    assert data["coverage"]["scenario"] == pytest.approx(1.0)
    assert data["coverage"]["win_rate"] == pytest.approx(1.0)
    assert data["coverage"]["judge_score"] == pytest.approx(round(1 / 8, 2))


def test_matrix_scenario_fallback_key_signature(dbs):
    """无 dashboard_id、无模板时：dashboard JSON key 签名兜底（candidate_stocks→筛选）。"""
    quality, base = dbs
    radar = sqlite3.connect(f"{base}/stock_radar.db")
    content = ('@@DASHBOARD_START@@{"candidate_stocks":[{"code":"600519"}],"index_data":null}'
               "@@DASHBOARD_END@@")
    _add_message(radar, 1, "d1", None, "agent_group", NOW - timedelta(days=1), content)
    radar.commit(); radar.close()
    data = qs.build_matrix(quality, days=90, now=NOW)
    assert data["cells"][0]["scenario"] == "筛选"
    assert data["cells"][0]["mode"] == "agent_group"


def test_matrix_all_dbs_none_empty():
    """四库全缺：cells 为空、覆盖率为 None，不抛异常（空数据不 500）。"""
    empty = qs.QualityDBs(signal=None, radar=None, trace=None, eval=None)
    data = qs.build_matrix(empty, days=90, now=NOW)
    assert data["cells"] == []
    assert data["coverage"]["scenario"] is None


def test_matrix_window_excludes_old_messages(dbs):
    """窗口外（>days 天前）消息不计入矩阵。"""
    quality, base = dbs
    radar = sqlite3.connect(f"{base}/stock_radar.db")
    _add_message(radar, 1, "d-old", None, "unified_plan", NOW - timedelta(days=91))
    _add_message(radar, 2, "d-new", None, "unified_plan", NOW - timedelta(days=1))
    radar.commit(); radar.close()
    data = qs.build_matrix(quality, days=90, now=NOW)
    assert sum(c["n"] for c in data["cells"]) == 1


# ── scenario 推断 ──────────────────────────────────────────────────

def test_infer_scenario_priority():
    assert qs.infer_scenario(dashboard_id="market", template_id="stock",
                             dashboard_keys={"candidate_stocks"}) == "大盘"  # 埋点优先
    assert qs.infer_scenario(dashboard_id=None, template_id="valuation_percentile",
                             dashboard_keys={"candidate_stocks"}) == "个股"   # 模板次之
    assert qs.infer_scenario(dashboard_id=None, template_id=None,
                             dashboard_keys={"index_data": 1, "price_levels": {}}) == "大盘"
    assert qs.infer_scenario(dashboard_id=None, template_id=None,
                             dashboard_keys={"price_levels": {}}) == "个股"
    assert qs.infer_scenario(dashboard_id="mystery", template_id=None,
                             dashboard_keys=set()) == "未知"


def test_dashboard_keys_extract():
    keys = qs._dashboard_keys('@@DASHBOARD_START@@{"a":null,"b":[1],"c":""}@@DASHBOARD_END@@')
    assert keys == {"b"}
    assert qs._dashboard_keys("无标记") == set()
    assert qs._dashboard_keys(None) is None
