"""Trace Viewer 上下文 API（GET /api/runs/{id}/context）的路由与载荷契约。

单测用 monkeypatch 替掉 `context_stats.build_run_context`，只验证「HTTP 边界」这一层：
① 载荷键集 = 真 build_run_context 的键集 − steps；② run 不存在时走 body 的 error 字段
（本端点**恒 200**，没有 404 语义）；③ steps 必须被剔除、其余字段一个不少；
④ run_id 为空串时不查库；⑤ do_GET 真的把 /context 路由到 _context_payload。

口径细节（六分类、成本、耗时归因）由 test_context_stats.py 负责，此处不重复。
"""
import http.client
import json
import socket
import sqlite3
import threading

import pytest

from utils.agent_trace import context_stats
from utils.agent_trace import server as viewer_server
from utils.agent_trace.models import ensure_month_schema, ensure_reviews_schema
from utils.agent_trace.store import TraceStore, store


class _FakeStore:
    """最小 store 桩：只为让**真** build_run_context 跑起来（不连库）。"""

    def __init__(self, run, steps):
        self._run, self._steps = run, steps

    def get_run(self, run_id):
        return self._run

    def get_steps_with_messages(self, run_id):
        return self._steps


def _real_snapshot() -> dict:
    """真 build_run_context 造一份最小快照 —— 各子结构形状的**唯一事实源**。

    这里刻意不复刻快照字面量：形状（键集）由实现产出，测试只比对。取值都取自真产出点，
    ⚠️ 不要编造（如 requests[] 里并不存在 index 字段、事件行也没有 type 字段）：
    * compress 埋点 —— agents/react/tool_node_base.py（raw_len/final_len/compress_method）
    * 注入标记 —— agents/base.py 的 [用户持仓账本]
    """
    run = {"id": "r1", "agent_name": "unified_plan", "status": "success",
           "duration_ms": 43218.56}
    steps = [
        {"id": 6, "event_run_id": "e1", "step_type": "tool", "step_name": "mx_data_query",
         "status": "success", "duration_ms": 1200,
         "extra": json.dumps({"label": "mx_data_query",
                              "compress": {"raw_len": 1028, "final_len": 792,
                                           "compress_method": "trim"}}),
         "messages": []},
        {"id": 7, "event_run_id": "e1", "step_type": "llm", "step_name": "agent",
         "status": "success", "duration_ms": 9000,
         "started_at": "2026-10-16T13:43:17.870681+00:00",
         "extra": json.dumps({"model_name": "deepseek-v4.1-flash", "label": "agent",
                              "token_usage": {"input_tokens": 19496, "output_tokens": 3352,
                                              "cached_tokens": 52480, "reasoning_tokens": 1083}}),
         "messages": [{"role": "human",
                       "content": "[用户持仓账本] 贵州茅台(600519) 成本1680.00×200股"}]},
    ]
    return context_stats.build_run_context("r1", store=_FakeStore(run, steps))


# 一份冻结快照（取值取自真实 run，形状与 build_run_context 一致）。
# steps 用 2 条带 messages 的合成步骤钉住「HTTP 边界要剔除它」这一行为。
# 形状漂移风险由 test_snapshot_fixture_matches_real_build_run_context_shape 兜住。
def _snapshot(rid: str) -> dict:
    return {
        "run": {"id": rid, "agent_name": "unified_plan", "status": "success",
                "duration_ms": 43218.56, "model": "deepseek-v4.1-flash"},
        "window": {"limit": 1000000, "source": "config"},
        "totals": {"billed_tokens": 78767, "input_tokens": 75415, "output_tokens": 3352,
                   "cached_tokens": 52480, "cache_hit_ratio": 0.6958827819399324,
                   "thinking_tokens": 1083, "answer_tokens": 2269,
                   "thinking_ratio_of_output": 0.323090692124105,
                   "current_input_tokens": 19496, "llm_calls": 7, "calls_with_thinking": 6,
                   "thinking_calls_coverage": 0.8571428571428571, "cost": 0.0371,
                   "tool_calls": 3, "steps": 2, "human_inputs": 7},
        "output_split": {"thinking": 1083, "answer": 2269},
        "requests": [{
            "step_id": 7, "event_run_id": "e1", "label": "agent", "node": "agent",
            "round_no": None, "phase": None, "kind": "main",
            "tokens": {"system": 15204, "tools": None, "user": 1420, "injected": 11210,
                       "assistant": 1873, "tool_results": 6774},
            "input_tokens": 19496, "output_tokens": 3352, "thinking_tokens": 1083,
            "answer_tokens": 2269, "cached_tokens": 52480,
            "cache_hit_ratio": 0.6958827819399324, "occupancy": 0.019496,
            "cost": 0.0371, "status": "success", "duration_ms": 9000,
        }],
        "composition": {"system": 15204, "tools": None, "user": 1420, "injected": 11210,
                        "assistant": 1873, "tool_results": 6774},
        "timing": {"wall_ms": 709874.8, "llm_ms": 777208.4800000001, "tool_ms": 68302.87,
                   "collector_ms": 0.0, "sum_ratio": 1.1911, "overlap_ms": 135636.55,
                   "by_type": {"graph": 709194.56, "llm": 777208.4800000001,
                               "tool": 68302.87}},
        # 事件行的两类真实形态：compress 比 inject 多一个 delta_chars
        "events": [
            {"step_id": 6, "label": "mx_data_query", "at": None, "kind": "compress",
             "detail": {"raw_len": 1028, "final_len": 792, "compress_method": "trim"},
             "delta_chars": -236},
            {"step_id": 7, "label": "agent", "at": "2026-10-16T13:43:17.870681+00:00",
             "kind": "inject", "detail": {"messages": 1}},
        ],
        "slowest_llm": [{"label": "agent", "node": "agent", "input_tokens": 19496,
                         "duration_ms": 9000, "ms_per_1k": 461.6}],
        "steps": [
            {"id": 6, "step_type": "tool", "messages": [{"role": "tool", "content": "y" * 50}]},
            {"id": 7, "step_type": "llm", "messages": [{"role": "user", "content": "x" * 50}]},
        ],
    }


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    """分库形态的临时 trace 环境（月库 r1 + 主库 reviews），避免碰真实库。"""
    db_path = str(tmp_path / "agent_trace.db")
    month_dir = tmp_path / "agent_trace"
    month_dir.mkdir()
    month_db = month_dir / "agent_trace_202609.db"

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_reviews_schema(conn)
    conn.commit()

    mconn = sqlite3.connect(str(month_db), check_same_thread=False)
    mconn.row_factory = sqlite3.Row
    ensure_month_schema(mconn)
    mconn.execute(
        "INSERT INTO runs(id,agent_name,input,status,created_at) "
        "VALUES('r1','react_stock','{}','success','2026-09-18T00:00:00')"
    )
    mconn.commit()

    monkeypatch.setattr(TraceStore, "main_db_path", lambda self: db_path)
    monkeypatch.setattr(TraceStore, "trace_dir", lambda self: str(month_dir))
    monkeypatch.setattr(viewer_server, "_resolved_db", db_path)
    yield db_path
    conn.close()
    mconn.close()
    store.close_thread_conns()


@pytest.fixture()
def http_env(trace_env):
    if viewer_server.DualStackHTTPServer.address_family == socket.AF_INET6:
        host = "::1"
    else:
        host = "127.0.0.1"
    httpd = viewer_server.DualStackHTTPServer((host, 0), viewer_server.TraceHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield host, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(host, port, path):
    client = http.client.HTTPConnection(host, port, timeout=30)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        client.close()


def test_snapshot_fixture_matches_real_build_run_context_shape():
    """护栏：冻结的 _snapshot 形状必须与真 build_run_context 的产出逐层对齐。

    context_stats 改了返回结构（加键 / 删键 / 改名字）时这条会红，逼人同步快照；
    否则 _snapshot 会越来越旧，后续用例全在比对一份过时的想象。
    """
    real, fake = _real_snapshot(), _snapshot("r1")
    assert set(fake) == set(real)
    for key in ("run", "window", "totals", "output_split", "composition", "timing"):
        assert set(fake[key]) == set(real[key]), key
    assert set(fake["requests"][0]) == set(real["requests"][0])
    assert set(fake["requests"][0]["tokens"]) == set(real["requests"][0]["tokens"])
    assert set(fake["slowest_llm"][0]) == set(real["slowest_llm"][0])
    # 事件行按 kind 有可选键（compress 多 delta_chars），故比"出现过的键的并集"
    assert (set().union(*(set(e) for e in fake["events"]))
            == set().union(*(set(e) for e in real["events"])))


def test_context_payload_key_set_is_real_snapshot_minus_steps(monkeypatch):
    """载荷键集 = 真 build_run_context 的键集 − steps（不是手工列举的字面量）。"""
    real = _real_snapshot()
    # 前提：任务 3/4/5 的内部约定是快照仍随返回体带 steps（HTTP 边界才剔除）
    assert "steps" in real
    monkeypatch.setattr(context_stats, "build_run_context",
                        lambda rid, store=None: dict(real))   # 浅拷贝：pop 不得污染真返回体
    payload = viewer_server._context_payload("r1")
    assert set(payload) == set(real) - {"steps"}


def test_context_endpoint_returns_error_body_when_run_missing(monkeypatch):
    """run 不存在时给 body 的 error 字段 —— 本端点恒 200，没有 404 语义。"""
    monkeypatch.setattr(context_stats, "build_run_context", lambda rid, store=None: None)
    assert viewer_server._context_payload("nope") == {"error": "run not found"}


def test_context_payload_strips_steps(monkeypatch):
    """steps 是服务端计算用的中间物，HTTP 响应体必须剔除它。

    实测（真实最大 run 95aaaf7e-367b-49d7-bccf-cbba52b0fb39，100 步 / 220 条消息，
    数字均为 UTF-8 字节，与 HTTP body 长度同口径）：带 steps 的 JSON 8,083,807 字节，
    其中 steps 键占 8,056,374 字节（99.7%）；pop 掉之后同一 run 的响应 27,422 字节
    （gzip 后 4,709 字节）—— 前后差约 295 倍。steps 不是"多带一个字段"，
    它本身就是这个东西的全部体积。
    """
    monkeypatch.setattr(context_stats, "build_run_context",
                        lambda rid, store=None: _snapshot(rid))
    payload = viewer_server._context_payload("r1")
    assert "steps" not in payload
    # 别的字段一个都不能少（剔除只针对 steps 这一个键）
    assert payload["totals"]["steps"] == 2      # 同名不同义：这是步数（数字）
    assert set(payload) == {"run", "window", "totals", "output_split", "requests",
                            "composition", "timing", "events", "slowest_llm"}
    # 六分类键集与 context_stats.CATEGORIES 对齐（任务 10 前端按它渲染构成图）
    assert set(payload["composition"]) == set(context_stats.CATEGORIES)


def test_context_payload_empty_run_id_short_circuits(monkeypatch):
    """空 run_id 不得落到 build_run_context：那会真去逐月库 probe 一次空 id，
    并把"缺参数"报成"run not found"（前端分不清两种错）。"""
    def boom(*a, **kw):
        raise AssertionError("空 run_id 不该查库")

    monkeypatch.setattr(context_stats, "build_run_context", boom)
    assert viewer_server._context_payload("") == {"error": "run_id required"}


def test_http_get_context_routes_to_payload(http_env, monkeypatch):
    """接线锁：do_GET 的 /api/runs/<id>/context 分支必须真的走 _context_payload。"""
    monkeypatch.setattr(context_stats, "build_run_context",
                        lambda rid, store=None: _snapshot(rid))
    status, raw = _get(*http_env, "/api/runs/r1/context")
    out = json.loads(raw)
    assert status == 200                      # viewer 一律 200，错误语义走 body
    assert out["run"]["id"] == "r1"
    assert "steps" not in out
    assert "timing" in out


def test_http_get_context_missing_run_returns_error_body(http_env, monkeypatch):
    monkeypatch.setattr(context_stats, "build_run_context", lambda rid, store=None: None)
    status, raw = _get(*http_env, "/api/runs/nope/context")
    assert status == 200
    assert json.loads(raw) == {"error": "run not found"}
