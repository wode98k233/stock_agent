"""成交口径（entry_timing / exit_timing）落库与三入口一致性单测。

背景：Phase 0 已修好撮合本身（same_close + next_open/next_close + hold_bars=1），
但口径字段**只传给后台线程、没落库**：面板/技能发起的任务在 ``strategy_run``
里查不到口径，续跑只能回退会话（面板任务根本没有会话），结果卡只能按默认值
渲染。本文件锁住四条契约：

1. ``strategy_run`` 建表/落库/读取都带这两列（加性迁移；旧库缺列时降级不崩）；
2. ``start_strategy_run`` 归一化后**既落库也传给后台线程**；
3. ``_resume_timings`` 优先任务行 → 原会话 → 默认；exit 空串跟随 entry；
4. 详情/结果卡 payload 带解析后的口径；前端选择器/请求体/资源版本已接上。

全部用临时库（不碰 db/backtest.db），后台线程一律替身，不外呼。
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: F401

ROOT = Path(PROJECT_ROOT)
SR_JS = ROOT / 'server/static/js/strategy-research.js'
CARD_JS = ROOT / 'server/static/js/chat-strategy-card.js'
INDEX_HTML = ROOT / 'server/static/index.html'


# ============================================================
# 夹具
# ============================================================


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """临时 backtest.db（迁移已跑的正常库）。"""
    from config import Config
    db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: db)
    from utils.cache import backtest_db as bd
    bd.init_backtest_tables()
    bd._MISSING_COLS.clear()
    yield db
    bd._MISSING_COLS.clear()


def _drop_column(db_path: str, table: str, column: str) -> None:
    """模拟「迁移还没跑」的库（SQLite 老版本无 DROP COLUMN，改 sqlite_master）。"""
    import sqlite3
    con = sqlite3.connect(db_path)
    ddl = [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))][0]
    body = ddl[ddl.index('(') + 1: ddl.rindex(')')]
    # 注释行原样保留（列定义与注释不在同一行）；按顶层逗号切分列定义
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from utils.db_schema.migrator import _split_top_level, _strip_sql_comments
    kept = []
    for part in _split_top_level(body):
        stripped = _strip_sql_comments(part).strip()
        if not stripped:
            continue
        if stripped.split()[0] == column:
            continue
        kept.append(stripped)
    con.execute('PRAGMA writable_schema = ON')
    con.execute("UPDATE sqlite_master SET sql=? WHERE name=?",
                ('CREATE TABLE %s (%s)' % (table, ', '.join(kept)), table))
    con.commit()
    con.close()
    con = sqlite3.connect(db_path)
    assert column not in [r[1] for r in con.execute('PRAGMA table_info(%s)' % table)]
    con.close()


def _fake_thread(monkeypatch, sr_mod):
    """把研发线程换成记录器：拿到 args 就地执行（不真起线程、不外呼）。"""
    calls = []
    monkeypatch.setattr(sr_mod, '_run_loop_async', lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(sr_mod, 'threading', SimpleNamespace(Thread=_FakeThread))
    return calls


class _FakeThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)


@pytest.fixture()
def sr_mod(tmp_db, monkeypatch):
    """策略研发路由模块：指数判定放行 + 线程替身。"""
    import server.routes.strategy_research as mod
    monkeypatch.setattr(mod, '_is_index_code', lambda code: False)
    return mod


# ============================================================
# 1. strategy_run 建表 / 落库 / 读取
# ============================================================


class TestStrategyRunSchema:
    def test_columns_exist_with_expected_defaults(self, tmp_db):
        from utils.cache.backtest_db import get_backtest_db
        with get_backtest_db() as conn:
            cols = {r[1]: dict(name=r[1], notnull=r[3], default=r[4])
                    for r in conn.execute('PRAGMA table_info(strategy_run)')}
        assert cols['entry_timing']['notnull'] == 1
        assert cols['entry_timing']['default'] == "'next_open'"
        assert cols['exit_timing']['notnull'] == 1
        assert cols['exit_timing']['default'] == "''"

    def test_create_and_read_roundtrip(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run, get_strategy_run
        create_strategy_run('SR-TIM-1', '尾盘买入次日开盘卖出', '600519',
                            '2023-01-01~2026-01-01',
                            entry_timing='same_close', exit_timing='next_open')
        row = get_strategy_run('SR-TIM-1')
        assert row['entry_timing'] == 'same_close'
        assert row['exit_timing'] == 'next_open'

    def test_default_create_follows_entry(self, tmp_db):
        """不传口径：买入口径 next_open，卖出口径空串（= 跟随 entry，不是 next_open）。"""
        from utils.cache.backtest_db import create_strategy_run, get_strategy_run
        create_strategy_run('SR-TIM-2', '默认口径', '600519', 'a~b')
        row = get_strategy_run('SR-TIM-2')
        assert row['entry_timing'] == 'next_open'
        assert row['exit_timing'] == ''

    def test_create_survives_missing_columns(self, tmp_db):
        """迁移没跑（旧库缺列）时降级为不写这几列，任务照建、读侧不崩。"""
        from utils.cache.backtest_db import create_strategy_run, get_strategy_run
        from server.routes.strategy_research import _timings_of_run
        _drop_column(tmp_db, 'strategy_run', 'entry_timing')
        _drop_column(tmp_db, 'strategy_run', 'exit_timing')
        row = create_strategy_run('SR-TIM-3', '旧库降级', '600519', 'a~b',
                                  entry_timing='same_close', exit_timing='next_close')
        assert row['status'] == 'running'
        # 缺列时**不补默认值**（数据缺失 ≠ 默认口径），由调用方归一化
        assert 'entry_timing' not in row and 'exit_timing' not in row
        assert _timings_of_run(row) == ('next_open', 'next_open')
        assert get_strategy_run('SR-TIM-3')['status'] == 'running'


# ============================================================
# 2. start_strategy_run：归一化 + 落库 + 透传线程
# ============================================================


class TestStartRunTiming:
    def _start(self, sr_mod, monkeypatch, **kw):
        calls = _fake_thread(monkeypatch, sr_mod)
        info = sr_mod.start_strategy_run('尾盘买入第二天开盘卖出', '600519', **kw)
        from utils.cache.backtest_db import get_strategy_run
        return info, get_strategy_run(info['run_uuid']), calls

    def test_persists_and_passes_timing(self, sr_mod, monkeypatch):
        info, row, calls = self._start(sr_mod, monkeypatch,
                                       entry_timing='same_close',
                                       exit_timing='next_close')
        assert row['entry_timing'] == 'same_close'
        assert row['exit_timing'] == 'next_close'
        assert len(calls) == 1
        assert calls[0][0] == info['run_uuid']
        assert calls[0][1:3] == ('same_close', 'next_close')

    def test_empty_exit_follows_entry(self, sr_mod, monkeypatch):
        """卖出口径留空要**原样落库为空串**（跟随买入），不能被悄悄写成 next_open。"""
        info, row, calls = self._start(sr_mod, monkeypatch, entry_timing='same_close')
        assert row['entry_timing'] == 'same_close'
        assert row['exit_timing'] == ''
        assert calls[0][1:3] == ('same_close', '')

    def test_illegal_values_normalized(self, sr_mod, monkeypatch):
        """非法口径回落 next_open：错口径比报错更危险（结论整体失真还看不出来）。"""
        info, row, calls = self._start(sr_mod, monkeypatch,
                                       entry_timing='tomorrow-morning',
                                       exit_timing='whenever')
        assert row['entry_timing'] == 'next_open'
        assert row['exit_timing'] == 'next_open'
        assert calls[0][1:3] == ('next_open', 'next_open')

    def test_defaults(self, sr_mod, monkeypatch):
        info, row, calls = self._start(sr_mod, monkeypatch)
        assert (row['entry_timing'], row['exit_timing']) == ('next_open', '')
        assert calls[0][1:3] == ('next_open', '')


# ============================================================
# 3. _resume_timings：任务行 → 会话 → 默认
# ============================================================


def _set_run_timings(run_uuid: str, entry: str, exit_: str) -> None:
    """绕过 update 白名单直接改口径列（模拟老行/异常数据）。"""
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        conn.execute(
            'UPDATE strategy_run SET entry_timing = ?, exit_timing = ? WHERE run_uuid = ?',
            (entry, exit_, run_uuid))


def _link_session(run_uuid: str, entry: str, exit_timing: str) -> None:
    from utils.cache.backtest_db import create_strategy_session, update_strategy_session
    create_strategy_session('ss-tim-1', 'dlg-tim', '尾盘买入次日开盘卖出')
    update_strategy_session('ss-tim-1', status='active', run_uuid=run_uuid,
                            entry_timing=entry, exit_timing=exit_timing)


class TestResumeTimings:
    def test_run_row_wins(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-1', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='next_close')
        _link_session('SR-RES-1', entry='next_open', exit_timing='next_open')
        assert _resume_timings('SR-RES-1') == ('same_close', 'next_close')

    def test_fallback_to_session_when_row_empty(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-2', '想法', '600519', 'a~b')
        _set_run_timings('SR-RES-2', '', '')
        _link_session('SR-RES-2', entry='next_close', exit_timing='same_close')
        assert _resume_timings('SR-RES-2') == ('next_close', 'same_close')

    def test_missing_columns_fallback_to_session(self, tmp_db):
        """迁移未跑的旧库（缺列）：不能把「没记录」当成 next_open，必须回退会话。"""
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-2B', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='next_close')
        _link_session('SR-RES-2B', entry='next_close', exit_timing='same_close')
        _drop_column(tmp_db, 'strategy_run', 'entry_timing')
        _drop_column(tmp_db, 'strategy_run', 'exit_timing')
        assert _resume_timings('SR-RES-2B') == ('next_close', 'same_close')

    def test_default_when_nothing_recorded(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-3', '想法', '600519', 'a~b')
        _set_run_timings('SR-RES-3', '', '')
        assert _resume_timings('SR-RES-3') == ('next_open', 'next_open')

    def test_empty_exit_follows_entry(self, tmp_db):
        """行里只记了买入口径：卖出跟随买入，不能回落成 next_open。"""
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-4', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='')
        assert _resume_timings('SR-RES-4') == ('same_close', 'same_close')

    def test_empty_exit_does_not_fall_back_to_session_exit(self, tmp_db):
        """任务行已有买入口径、卖出口径为空时，空串=跟随买入，不能被会话的 exit 覆盖。"""
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _resume_timings
        create_strategy_run('SR-RES-4B', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='')
        _link_session('SR-RES-4B', entry='next_open', exit_timing='next_close')
        assert _resume_timings('SR-RES-4B') == ('same_close', 'same_close')

    def test_resume_api_uses_row_timing(self, tmp_db, sr_mod, monkeypatch):
        """续跑端点起线程时用的口径 = 任务行口径（不是会话/默认）。"""
        from utils.cache.backtest_db import (
            create_strategy_run, insert_strategy_iter, update_strategy_run,
        )
        create_strategy_run('SR-RES-5', '想法', '600519', '2023-01-01~2026-01-01',
                            entry_timing='same_close', exit_timing='next_close')
        insert_strategy_iter('SR-RES-5', round_no=1, hypothesis='h',
                             strategy_json='{"meta": {"name": "x"}}',
                             strategy_hash='h1', strategy_diff='',
                             compile_ok=1, compile_error=None,
                             is_metrics={'sharpe': 1.0},
                             oos_metrics={'sharpe': 0.9, 'trades': 10}, verdict='keep')
        update_strategy_run('SR-RES-5', status='interrupted')
        _link_session('SR-RES-5', entry='next_open', exit_timing='next_open')

        calls = _fake_thread(monkeypatch, sr_mod)
        asyncio.run(sr_mod.resume_run('SR-RES-5'))
        assert len(calls) == 1
        assert calls[0][1:3] == ('same_close', 'next_close')


# ============================================================
# 4. 详情 / 结果卡 payload
# ============================================================


class TestDetailPayloadTiming:
    def test_detail_payload_has_resolved_timing(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _run_detail_payload
        create_strategy_run('SR-DET-1', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='next_open')
        data = _run_detail_payload('SR-DET-1')
        assert data['entry_timing'] == 'same_close'
        assert data['exit_timing'] == 'next_open'

    def test_detail_payload_follows_entry_when_exit_empty(self, tmp_db):
        from utils.cache.backtest_db import create_strategy_run
        from server.routes.strategy_research import _run_detail_payload
        create_strategy_run('SR-DET-2', '想法', '600519', 'a~b',
                            entry_timing='same_close', exit_timing='')
        data = _run_detail_payload('SR-DET-2')
        assert (data['entry_timing'], data['exit_timing']) == ('same_close', 'same_close')

    def test_card_payload_carries_exit_timing(self, tmp_db):
        """对话卡（/card）也要带卖出侧：只给 entry 时卡片写不全「尾盘买 → 次日开盘卖」。"""
        from utils.cache.backtest_db import create_strategy_run, update_strategy_run
        from server.routes.strategy_research import get_run_card
        create_strategy_run('SR-CARD-1', '想法', '600519', '2023-01-01~2026-01-01',
                            entry_timing='same_close', exit_timing='next_open')
        update_strategy_run('SR-CARD-1', status='done', stop_reason='converged')
        res = asyncio.run(get_run_card('SR-CARD-1'))
        payload = res['data']['payload']
        assert res['data']['kind'] == 'result'
        assert payload['entry_timing'] == 'same_close'
        assert payload['exit_timing'] == 'next_open'


# ============================================================
# 5. 前端接线（选择器 / 请求体 / 资源版本）
# ============================================================


def _text(p: Path) -> str:
    return p.read_text(encoding='utf-8')


class TestFrontendWiring:
    def test_form_has_both_timing_selects(self):
        js = _text(SR_JS)
        assert "id='srEntryTiming'" in js
        assert "id='srExitTiming'" in js
        # 卖出口径必须有「跟随买入」空值选项（默认值）
        assert re.search(r"<option value=''>跟随买入</option>", js)

    def test_start_run_body_passes_timing(self):
        js = _text(SR_JS)
        assert 'entry_timing: entryTiming' in js
        assert 'exit_timing: exitTiming' in js
        assert '$("#srEntryTiming"' in js and '$("#srExitTiming"' in js

    def test_detail_shows_timing(self):
        js = _text(SR_JS)
        assert 'timingText(' in js and '成交口径：' in js

    def test_chat_card_reads_run_detail_timing(self):
        js = _text(CARD_JS)
        assert 'timingLabel(payload.entry_timing, payload.exit_timing)' in js
        # 对话正文卡片只有买入侧 → 按 run_uuid 拉 run detail 补卖出侧（带缓存）
        assert '/api/strategy-research/runs/' in js
        assert 'timingDetailCache' in js
        assert '成交口径：' in js

    def test_index_versions_bumped(self):
        html = _text(INDEX_HTML)
        m1 = re.search(r'/static/js/strategy-research\.js\?v=(\d+)', html)
        m2 = re.search(r'/static/js/chat-strategy-card\.js\?v=(\d+)', html)
        assert m1 and int(m1.group(1)) >= 47
        assert m2 and int(m2.group(1)) >= 11
