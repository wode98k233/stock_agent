"""任务 6（策略信号带身份）单测：signal 三列迁移 + 采纳回写 strategy_hash + track 身份三列。

覆盖（计划步骤 6.7，另加一条 track 身份断言）：
1. 新库建表即含 source/strategy_id/ref_run_id，默认 llm/''
2. 旧表（无三列、单键 UNIQUE）迁移后可写读身份三列（重建表后仍保留）
3. update_strategy_hash 不存在时 _adopt_save 不炸（跨代理依赖未落地也安全）
4. update_strategy_hash：命中行 True 且刷新 updated_at，未命中行 False
5. _adopt_save 把最优 iter 的 strategy_hash 写进 strategies（打桩断言入参）
6. best_iter_id=None → 不调用 update_strategy_hash，仍返回 code=0
7. _adopt_track 写入 source='strategy' / strategy_id / ref_run_id（dialog_uuid 保持原样）

全部用临时库，不碰真实数据、不联网。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from signal_eval.db import get_conn, init_db, close_conn  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_conn():
    """每个测试前后重置 signal_eval 线程本地连接，避免 monkeypatch 路径被缓存污染。"""
    close_conn()
    yield
    close_conn()


@pytest.fixture()
def isolated_dbs(tmp_path, monkeypatch):
    """临时 backtest.db + signal_eval.db（不碰真库）。"""
    bt_db = str(tmp_path / 'backtest.db')
    se_db = str(tmp_path / 'signal_eval.db')

    monkeypatch.setattr('config.Config.get_backtest_db_path', lambda: bt_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()

    monkeypatch.setattr('signal_eval.db.SIGNAL_EVAL_DB_PATH', se_db)
    close_conn()
    init_db()
    yield {'bt_db': bt_db, 'se_db': se_db}
    close_conn()


@pytest.fixture()
def done_run(isolated_dbs):
    """status=done 且有最优轮次的研发任务（合法可编译策略 JSON）。"""
    from utils.cache.backtest_db import (
        create_strategy_run, insert_strategy_iter, update_strategy_run,
    )
    strategy = {
        'meta': {'name': '测试策略', 'description': 'MA 金叉', 'tags': []},
        'params': {'fast': {'value': 5, 'min': 2, 'max': 60, 'step': 1, 'label': '快线', 'type': 'int'},
                   'slow': {'value': 20, 'min': 5, 'max': 200, 'step': 1, 'label': '慢线', 'type': 'int'}},
        'conditions': {
            'buy': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_above', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
            'sell': {'logic': 'AND', 'rules': [
                {'type': 'indicator', 'left': {'func': 'ma', 'args': ['close', '{fast}']},
                 'op': 'cross_below', 'right': {'func': 'ma', 'args': ['close', '{slow}']}}]},
        },
        'risk': {'stop_loss': {'type': 'fixed', 'value': 0.08}},
    }
    create_strategy_run('SR-ADOPT', '测试想法', '600519', '2023-01-01~2026-01-01',
                        source='panel', dialog_uuid='dlg-x')
    it_id = insert_strategy_iter('SR-ADOPT', round_no=1, hypothesis='假设',
                                 strategy_json=json.dumps(strategy, ensure_ascii=False),
                                 strategy_hash='hash-of-best', strategy_diff='首轮生成',
                                 compile_ok=1, compile_error=None,
                                 is_metrics={'sharpe': 1.0},
                                 oos_metrics={'sharpe': 0.9, 'trades': 10},
                                 overfit_flag=0, verdict='keep', model='m')
    update_strategy_run('SR-ADOPT', status='done', stop_reason='converged',
                        best_iter_id=it_id, total_iters=1)
    return 'SR-ADOPT'


def _cols(conn) -> list:
    return [r[1] for r in conn.execute('PRAGMA table_info(signal)').fetchall()]


def _indexes(conn) -> list:
    return [r[1] for r in conn.execute('PRAGMA index_list(signal)').fetchall()]


# ── 迁移 ────────────────────────────────────────────────────────────

class TestSignalIdentityMigration:
    def test_new_table_has_identity_columns_and_is_idempotent(self, tmp_path, monkeypatch):
        db = tmp_path / 'signal_eval.db'
        monkeypatch.setattr('signal_eval.db.SIGNAL_EVAL_DB_PATH', str(db))
        conn = get_conn()
        try:
            cols = _cols(conn)
            assert 'source' in cols and 'strategy_id' in cols and 'ref_run_id' in cols
            assert 'idx_signal_strategy' in _indexes(conn)
            # 新写入行默认 source='llm'、两个 ID 为空串
            conn.execute("INSERT INTO signal(dialog_uuid,message_uuid,decision,signal_time) "
                         "VALUES('d','m1','buy','2026-09-01 10:00:00')")
            row = conn.execute("SELECT source, strategy_id, ref_run_id FROM signal "
                               "WHERE message_uuid='m1'").fetchone()
            assert (row['source'], row['strategy_id'], row['ref_run_id']) == ('llm', '', '')
            init_db()
            init_db()          # 幂等：重复迁移不报错
            assert 'idx_signal_strategy' in _indexes(conn)
        finally:
            conn.close()

    def test_legacy_table_migrated_then_identity_readable(self, tmp_path, monkeypatch):
        """旧表（无三列、message_uuid 单键 UNIQUE）→ 迁移后可写读身份三列。"""
        db = tmp_path / 'signal_eval.db'
        raw = sqlite3.connect(str(db))
        raw.execute(
            "CREATE TABLE signal ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_uuid TEXT NOT NULL, "
            "message_uuid TEXT NOT NULL UNIQUE, symbol_key TEXT, symbol_name TEXT, "
            "decision TEXT NOT NULL CHECK (decision IN ('buy','sell','hold')), "
            "confidence REAL, sentiment REAL, core_verdict TEXT, signal_time TEXT NOT NULL)")
        raw.execute("INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time) "
                    "VALUES('d','old-1','sh600519','buy','2026-08-01 10:00:00')")
        raw.commit()
        raw.close()

        monkeypatch.setattr('signal_eval.db.SIGNAL_EVAL_DB_PATH', str(db))
        conn = get_conn()      # 触发迁移链（含重建表 + 补三列）
        try:
            assert {'source', 'strategy_id', 'ref_run_id'} <= set(_cols(conn))
            assert 'idx_signal_strategy' in _indexes(conn)
            old = conn.execute("SELECT symbol_key, source, strategy_id, ref_run_id FROM signal "
                               "WHERE message_uuid='old-1'").fetchone()
            assert old['symbol_key'] == 'sh600519'
            # 旧行按列默认值补 'llm'（不误标成策略信号）
            assert (old['source'], old['strategy_id'], old['ref_run_id']) == ('llm', '', '')

            conn.execute(
                "INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time,"
                "source,strategy_id,ref_run_id) VALUES('','sr_SR-X','sh600519','buy',"
                "'2026-09-06 16:07:46','strategy','sr_x','SR-X')")
            conn.commit()
            got = conn.execute("SELECT strategy_id, ref_run_id, source FROM signal "
                               "WHERE message_uuid='sr_SR-X'").fetchone()
            assert (got['source'], got['strategy_id'], got['ref_run_id']) == ('strategy', 'sr_x', 'SR-X')
            init_db()          # 幂等
        finally:
            conn.close()

    def test_backfill_relabels_legacy_adopt_signals(self, tmp_path, monkeypatch):
        """加列前写入的策略信号（source 默认 llm）→ 迁移按 sr_ 签名回填身份，幂等。"""
        db = tmp_path / 'signal_eval.db'
        raw = sqlite3.connect(str(db))
        raw.execute(
            "CREATE TABLE signal ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_uuid TEXT NOT NULL, "
            "message_uuid TEXT NOT NULL UNIQUE, symbol_key TEXT, symbol_name TEXT, "
            "decision TEXT NOT NULL CHECK (decision IN ('buy','sell','hold')), "
            "confidence REAL, sentiment REAL, core_verdict TEXT, signal_time TEXT NOT NULL)")
        raw.execute("INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time) "
                    "VALUES('sr_SR-20260906-AAFDE8','sr_SR-20260906-AAFDE8','sh600519','buy',"
                    "'2026-09-06 16:07:46')")
        raw.execute("INSERT INTO signal(dialog_uuid,message_uuid,symbol_key,decision,signal_time) "
                    "VALUES('d','llm-1','sh600519','buy','2026-09-06 16:07:46')")
        raw.commit()
        raw.close()

        monkeypatch.setattr('signal_eval.db.SIGNAL_EVAL_DB_PATH', str(db))
        conn = get_conn()      # 触发迁移链
        try:
            got = conn.execute("SELECT source, strategy_id, ref_run_id FROM signal "
                               "WHERE message_uuid='sr_SR-20260906-AAFDE8'").fetchone()
            # 与 _adopt_track 同口径：末段小写
            assert (got['source'], got['strategy_id'], got['ref_run_id']) == (
                'strategy', 'sr_aafde8', 'SR-20260906-AAFDE8')
            llm = conn.execute("SELECT source, strategy_id FROM signal "
                               "WHERE message_uuid='llm-1'").fetchone()
            assert (llm['source'], llm['strategy_id']) == ('llm', '')   # 对话信号不动
            init_db()          # 幂等：再迁移一次不改变结果
            again = conn.execute("SELECT strategy_id FROM signal "
                                 "WHERE message_uuid='sr_SR-20260906-AAFDE8'").fetchone()
            assert again['strategy_id'] == 'sr_aafde8'
        finally:
            conn.close()


# ── strategies.strategy_hash 回写 ───────────────────────────────────

class TestStrategyHashWriteback:
    def test_update_strategy_hash_true_and_false(self, isolated_dbs):
        from utils.cache.backtest_db import get_backtest_db, get_strategy, update_strategy_hash
        sid = 'sr_unit_hash'
        with get_backtest_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategies (strategy_id, name, description, category, "
                "is_builtin, code, params_schema, tags, created_at, updated_at, strategy_hash) "
                "VALUES (?, ?, '', 'custom', 0, '{}', '[]', '[]', "
                "'2000-01-01 00:00:00', '2000-01-01 00:00:00', '')", (sid, '单测策略'))
        assert update_strategy_hash(sid, 'abc123') is True
        saved = get_strategy(sid)
        assert saved['strategy_hash'] == 'abc123'
        assert saved['updated_at'] != '2000-01-01 00:00:00'    # 顺带刷新 updated_at
        assert update_strategy_hash('sr_不存在', 'x') is False  # 未命中行

    def test_adopt_save_writes_hash_from_best_iter(self, isolated_dbs, done_run, monkeypatch):
        import utils.cache.backtest_db as bdb
        from server.routes.strategy_research import _adopt_save

        run = bdb.get_strategy_run(done_run)
        best = bdb.get_strategy_iter_by_id(run['best_iter_id'])   # 先取真 iter（含 strategy_json）

        calls = []
        monkeypatch.setattr(bdb, 'get_strategy_iter_by_id',
                            lambda iter_id: {'id': iter_id, 'strategy_hash': 'abc123'})
        monkeypatch.setattr(bdb, 'update_strategy_hash',
                            lambda sid, h: (calls.append((sid, h)) or True), raising=False)

        res = _adopt_save(run, best)
        assert res['code'] == 0
        assert calls == [(res['data']['strategy_id'], 'abc123')]
        assert bdb.get_strategy(res['data']['strategy_id']) is not None

    def test_adopt_save_without_best_iter_skips_hash(self, isolated_dbs, done_run, monkeypatch):
        import utils.cache.backtest_db as bdb
        from server.routes.strategy_research import _adopt_save

        run = bdb.get_strategy_run(done_run)
        best = bdb.get_strategy_iter_by_id(run['best_iter_id'])
        calls = []
        monkeypatch.setattr(bdb, 'update_strategy_hash',
                            lambda sid, h: (calls.append((sid, h)) or True), raising=False)

        run_no_best = dict(run)
        run_no_best['best_iter_id'] = None
        res = _adopt_save(run_no_best, best)
        assert res['code'] == 0
        assert calls == []

    def test_adopt_save_without_update_strategy_hash_still_ok(self, isolated_dbs, done_run,
                                                              monkeypatch):
        """跨代理依赖：update_strategy_hash 尚未落地时采纳不得失败（try/except 兜底）。"""
        import utils.cache.backtest_db as bdb
        from server.routes.strategy_research import _adopt_save

        run = bdb.get_strategy_run(done_run)
        best = bdb.get_strategy_iter_by_id(run['best_iter_id'])
        monkeypatch.delattr(bdb, 'update_strategy_hash', raising=False)

        res = _adopt_save(run, best)
        assert res['code'] == 0
        assert res['data']['strategy_id'].startswith('sr_')
        assert bdb.get_strategy(res['data']['strategy_id']) is not None


# ── _adopt_track 身份三列 ───────────────────────────────────────────

class TestAdoptTrackIdentity:
    def test_track_writes_source_and_strategy_id(self, isolated_dbs, done_run):
        from utils.cache.backtest_db import get_strategy_run, get_strategy_iter_by_id
        from server.routes.strategy_research import _adopt_track

        run = get_strategy_run(done_run)
        best = get_strategy_iter_by_id(run['best_iter_id'])
        res = _adopt_track(run, best)
        assert res['code'] == 0
        assert res['data']['message_uuid'] == 'sr_SR-ADOPT'

        rec = dict(get_conn().execute(
            "SELECT * FROM signal WHERE message_uuid='sr_SR-ADOPT'").fetchone())
        assert rec['source'] == 'strategy'
        assert rec['strategy_id'] == 'sr_adopt'      # 与 _adopt_save 同口径
        assert rec['ref_run_id'] == 'SR-ADOPT'
        assert rec['dialog_uuid'] == 'dlg-x'         # 既有行为不回退
        assert rec['confidence'] is None             # 不污染 A2 校准样本
        assert rec['symbol_key'] == 'sh600519'
