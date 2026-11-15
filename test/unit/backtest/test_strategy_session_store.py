"""strategy_session 表 CRUD 单测（Plan 2 任务 2）。

不连真库：每个用例把 backtest.db 指向 tmp_path 下的临时文件，跑完随 pytest 清理。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_backtest_db(tmp_path, monkeypatch):
    """把回测库指向临时文件并建表（范式同 test_backtest_ledger_health.py）"""
    from config import Config
    test_db = str(tmp_path / 'backtest.db')
    monkeypatch.setattr(Config, 'get_backtest_db_path', lambda: test_db)
    from utils.cache.backtest_db import init_backtest_tables
    init_backtest_tables()
    return test_db


class _TickingDatetime(datetime):
    """假时钟：每次 now() 前进 1 秒。

    updated_at 精度是秒，「先建 A 再建 B」「建完立刻更新」都会落在同一秒，
    排序与变更断言不可判定，故时间敏感用例换掉模块级 datetime。
    """
    _tick = 0

    @classmethod
    def now(cls, tz=None):
        cls._tick += 1
        return datetime(2026, 1, 1, 0, 0, 0) + timedelta(seconds=cls._tick)


@pytest.fixture()
def tick_clock(monkeypatch):
    """把 utils.cache.backtest_db 的模块级 datetime 换成递增假时钟"""
    from utils.cache import backtest_db
    monkeypatch.setattr(backtest_db, 'datetime', _TickingDatetime)


def test_create_session_defaults_pending_confirm(tmp_backtest_db):
    from utils.cache.backtest_db import create_strategy_session, get_strategy_session
    s = create_strategy_session('ss-1', 'dlg-1', '尾盘买入第二天卖出这个策略如何', opened_by='rule',
                                intent_confidence=0.75, intent_signals={'intent_words': ['策略']},
                                slots={'universe': '600519', 'entry_timing': 'same_close'})
    again = get_strategy_session('ss-1')
    assert s['status'] == 'pending_confirm'
    assert again['turn_count'] == 0
    assert again['slots_json'] == s['slots_json']
    assert again['entry_timing'] == 'same_close'  # slots 里的口径落到了独立列
    assert json.loads(again['intent_signals']) == {'intent_words': ['策略']}


def test_get_open_session_returns_latest(tmp_backtest_db, tick_clock):
    from utils.cache.backtest_db import create_strategy_session, get_open_strategy_session
    create_strategy_session('ss-early', 'dlg-1', '想法一')
    create_strategy_session('ss-late', 'dlg-1', '想法二')
    row = get_open_strategy_session('dlg-1')
    assert row is not None
    assert row['session_uuid'] == 'ss-late'


def test_get_open_session_ignores_archived(tmp_backtest_db):
    from utils.cache.backtest_db import (
        archive_strategy_session, create_strategy_session,
        get_open_strategy_session, get_strategy_session,
    )
    create_strategy_session('ss-arch', 'dlg-1', '想法')
    archive_strategy_session('ss-arch')
    assert get_open_strategy_session('dlg-1') is None
    assert get_strategy_session('ss-arch') is not None  # 归档 ≠ 删除


def test_update_session_whitelist_blocks_unknown(tmp_backtest_db):
    from utils.cache.backtest_db import (
        create_strategy_session, get_strategy_session, update_strategy_session,
    )
    create_strategy_session('ss-wl', 'dlg-1', '想法')
    update_strategy_session('ss-wl', status='active', hack='x')  # 未知字段不抛错、不落库
    row = get_strategy_session('ss-wl')
    assert row['status'] == 'active'
    assert 'hack' not in row


def test_update_session_touches_updated_at(tmp_backtest_db, tick_clock):
    from utils.cache.backtest_db import (
        create_strategy_session, get_strategy_session, update_strategy_session,
    )
    created = create_strategy_session('ss-ts', 'dlg-1', '想法')
    update_strategy_session('ss-ts', turn_count=1)
    row = get_strategy_session('ss-ts')
    assert row['turn_count'] == 1
    assert row['updated_at'] > created['updated_at']


def test_slots_json_roundtrip(tmp_backtest_db):
    """嵌套形状（strategy_gate._resolve_slots 产出）的中文 slots 原样存取 + 列抽取"""
    from utils.cache.backtest_db import create_strategy_session, get_strategy_session
    slots = {'universe': {'value': '贵州茅台', 'source': 'user'},
             'period': {'value': ['2023-01-01', '2026-01-01'], 'source': 'recommended'},
             'entry': {'value': '尾盘买入', 'source': 'recommended'}}
    create_strategy_session('ss-cn', 'dlg-1', '想法', slots=slots)
    row = get_strategy_session('ss-cn')
    assert json.loads(row['slots_json']) == slots          # 中文原样存取（ensure_ascii=False）
    assert '\\u8d35' not in row['slots_json']              # 没被转义成 \\uXXXX
    assert row['universe'] == '贵州茅台'                     # 嵌套槽位落到独立列
    assert row['period_start'] == '2023-01-01'
    assert row['period_end'] == '2026-01-01'


def test_archive_session(tmp_backtest_db):
    from utils.cache.backtest_db import archive_strategy_session, get_strategy_session
    from utils.cache.backtest_db import create_strategy_session
    create_strategy_session('ss-a', 'dlg-1', '想法')
    archive_strategy_session('ss-a')
    assert get_strategy_session('ss-a')['status'] == 'archived'


def test_dialog_uuid_isolation(tmp_backtest_db):
    from utils.cache.backtest_db import create_strategy_session, get_open_strategy_session
    create_strategy_session('ss-da', 'dlg-a', '想法 A')
    create_strategy_session('ss-db', 'dlg-b', '想法 B')
    assert get_open_strategy_session('dlg-a')['session_uuid'] == 'ss-da'
    assert get_open_strategy_session('dlg-b')['session_uuid'] == 'ss-db'
    assert get_open_strategy_session('dlg-missing') is None
