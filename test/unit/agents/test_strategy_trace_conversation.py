"""策略会话轮的 trace 正文：用户原话 + 助手回复都要落 step。"""
import asyncio

import pytest

from agents.shared import strategy_session_runner as ssr


class _Step:
    def __init__(self, name, input_data, sink):
        self.name = name
        self.input = input_data
        self.output = None
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_output(self, output):
        self.output = output
        self._sink.append((self.name, self.input, self.output))


class _Scope:
    def __init__(self):
        self.steps = []

    def step(self, name, input_data=None, extra=None):
        return _Step(name, input_data, self.steps)


class _Guard:
    def __init__(self, scope):
        self.scope = scope
        self.closed = False

    def close(self):
        self.closed = True


def _run(monkeypatch, scope, handler):
    guard = _Guard(scope)
    monkeypatch.setattr(ssr, '_TraceGuard', lambda session, text: guard)
    return asyncio.run(ssr._run_in_trace({}, '用户原话', handler)), guard


def test_records_user_input_and_assistant_reply(monkeypatch):
    async def handler():
        return '助手回复'

    scope = _Scope()
    result, guard = _run(monkeypatch, scope, handler)
    assert result == '助手回复'
    assert guard.closed is True
    assert scope.steps == [
        ('用户输入', {'input': '用户原话'}, {'input': '用户原话'}),
        ('助手回复', None, {'reply': '助手回复'}),
    ]


def test_no_scope_still_runs_business(monkeypatch):
    called = []

    async def handler():
        called.append(1)
        return 'ok'

    result, guard = _run(monkeypatch, None, handler)
    assert result == 'ok'
    assert called == [1]
    assert guard.closed is True


def test_error_still_closes_and_keeps_user_input(monkeypatch):
    async def handler():
        raise RuntimeError('boom')

    scope = _Scope()
    with pytest.raises(RuntimeError):
        _run(monkeypatch, scope, handler)
    assert scope.steps == [
        ('用户输入', {'input': '用户原话'}, {'input': '用户原话'}),
    ]


def test_reply_is_truncated(monkeypatch):
    async def handler():
        return 'x' * 9000

    scope = _Scope()
    _run(monkeypatch, scope, handler)
    assert len(scope.steps[1][2]['reply']) == 8000
