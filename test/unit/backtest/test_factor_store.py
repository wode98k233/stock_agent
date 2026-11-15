"""factor_store 持久化测试（工作台 v3 · F1）。

用临时 SQLite + 临时目录，不碰生产库。真实 DDL 直接从 TableDef 取——
这样表结构改了而测试没改时会立刻失败，而不是悄悄漂移。
"""
import contextlib
import json
import sqlite3

import pytest

from backtest import factor_store
from utils.db_schema.tables.backtest import TABLES


def _payload():
    """最小合法 payload（factor_eval.evaluate_factors 的真实形状子集）。"""
    return {
        "ranking": [
            {"rank": 1, "factor_id": "rsi_14", "name": "RSI(14)", "rank_ic_mean": -0.031,
             "dsr": 0.001},
            {"rank": 2, "factor_id": "ret_20", "name": "20 日动量", "rank_ic_mean": -0.0285,
             "dsr": 0.0},
        ],
        "significance": {"n_trials": 2, "sr_std": 0.045},
        "coverage": {"evaluated": 2, "n_codes": 6, "n_dates": 80},
        "params": {"ret_period": 5},
    }


def _params():
    return {"ret_period": 5, "neutralize": "both", "index_code": "000300"}


def _meta():
    return {"index_code": "000300", "start": "2024-10-01", "end": "2026-10-17"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "bt.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # 只建 factor_report 自己的表和索引——别去执行其它 TableDef 的索引，
    # 它们引用的表（如 data_tasks）在本测试库里并不存在。
    for td in TABLES:
        if td.name == "factor_report":
            conn.execute(td.ddl)
            for idx in getattr(td, "indexes", None) or []:
                conn.execute(idx)
    conn.commit()

    @contextlib.contextmanager
    def factory():
        yield conn

    monkeypatch.setattr(factor_store, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(factor_store, "_default_db", lambda: factory())
    return {"conn": conn, "tmp": tmp_path, "factory": factory}


def test_table_registered_in_backtest_db():
    names = [td.name for td in TABLES]
    assert "factor_report" in names, "factor_report 未注册进 backtest 库"


def test_save_and_list(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta())
    assert uuid.startswith("FR-")
    rows = factor_store.list_reports()
    assert len(rows) == 1
    r = rows[0]
    assert r["report_uuid"] == uuid
    assert r["kpi"]["top_factor"] == "RSI(14)"
    assert r["kpi"]["dsr_pass"] == 0
    assert r["n_factors"] == 2
    assert r["params"]["neutralize"] == "both"


def test_payload_roundtrip(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta())
    got = factor_store.load_payload(uuid)
    assert got["ranking"][0]["factor_id"] == "rsi_14"
    # 摘要与 payload 一致（列表页和详情页不能各说各话）
    row = factor_store.get_report(uuid)
    assert row["kpi"]["n_factors"] == got["coverage"]["evaluated"]


def test_html_roundtrip(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta(),
                                    html="<html>报告</html>")
    assert factor_store.load_html(uuid) == "<html>报告</html>"
    assert "html_path" in factor_store.get_report(uuid)


def test_delete_removes_row_and_files(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta(),
                                    html="<html>x</html>")
    row = factor_store.get_report(uuid)
    from backtest.factor_store import _abs
    assert _abs(row["payload_path"]).exists()
    assert factor_store.delete_report(uuid) is True
    assert factor_store.get_report(uuid) is None
    assert not _abs(row["payload_path"]).exists()
    assert not _abs(row["html_path"]).exists()
    assert factor_store.delete_report(uuid) is False, "重复删除应返回 False 而不是抛错"


def test_missing_payload_returns_none(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta())
    # 手动删文件，模拟「数据已清理」
    from backtest.factor_store import _abs
    _abs(factor_store.get_report(uuid)["payload_path"]).unlink()
    assert factor_store.load_payload(uuid) is None
    assert factor_store.get_report(uuid) is not None, "摘要记录还在（列表可见）"


def test_unknown_report(env):
    assert factor_store.get_report("FR-NOT-EXIST") is None
    assert factor_store.load_payload("FR-NOT-EXIST") is None


def test_payload_is_valid_json_file(env):
    uuid = factor_store.save_report(payload=_payload(), params=_params(), meta=_meta())
    from backtest.factor_store import _abs
    data = json.loads(_abs(factor_store.get_report(uuid)["payload_path"]).read_text(encoding="utf-8"))
    assert data["significance"]["n_trials"] == 2
