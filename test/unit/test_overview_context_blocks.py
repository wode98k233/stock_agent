"""总览大屏三块（上下文洞察 / 活跃度 / 会话上下文档案）后端单测。

口径事实源（步骤 1 已逐条打印核对，本文件按实际键名取值）：
- 上下文洞察 = utils/agent_trace/context_stats.py::build_context_insight：本文件只验证
  loader 委托它，并且没有在 loader 里重写空区间护栏。空区间的 None 护栏由
  context_stats 自身保证（判据是分母 input_tokens > 0；cost 是三态）。
- 活跃度 = calendar_query.query_trace_calendar 的 run_days 键（真实返回是
  {"year","month","run_days":{日期: [run 字典, ...]}}，不是 dialog_days），
  run 字典里取 total_tokens。
- 会话档案 = web_storage.list_dialogs + latest_trace_runs_for_dialogs（会话↔run 的现成映射）。
- _store_totals = store.llm_steps_totals，其真实返回是
  {run_id: [{"step_name","extra"(JSON 字符串)}, ...]} —— 逐 step 列表，不是聚合好的 dict。
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from server import overview_service as osvc


# ── 桩：store.aggregate_llm_usage 的空区间形态（与 store.py 实际返回键一致） ──

def _usage_agg(days: int = 7) -> dict:
    return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "reasoning_tokens": 0, "cache_hit_ratio": 0.0,
            "avg_tokens_per_call": 0, "daily": [0] * days, "by_agent": [], "by_model": [],
            "cost_total": 0.0, "cost_unpriced_calls": 0, "cost_missing_calls": 0,
            "cost_daily": [0.0] * days}


def _patch_usage(monkeypatch, agg: dict):
    """把 trace store 的聚合与 run 列表换成内存桩（生产代码是函数内 import，patch 类方法即可）。"""
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.list_runs",
                        lambda self, **kw: [])
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.aggregate_llm_usage",
                        lambda self, since="", days=14: agg)


# ── 块 1：上下文洞察（委托 build_context_insight，禁止在 loader 里重复护栏） ──

def test_load_context_insight_empty_range_yields_none_not_zero(monkeypatch):
    """空区间：cache_hit_ratio / cost 必须是 None（未采集），不能被 loader 折成 0。"""
    _patch_usage(monkeypatch, _usage_agg(14))
    out = osvc._load_context_insight({})
    assert out["kpi"]["cache_hit_ratio"] is None
    assert out["kpi"]["cost"] is None
    assert out["kpi"]["billed_tokens"] == 0


def test_load_context_insight_passes_config_days_through(monkeypatch):
    """days 透传自 Config.OVERVIEW_TOKEN_DAYS —— **用户可在配置面板改**（config_schema「全局总览」组）。

    ⚠️ 锁的是「loader 把配置值透传给 build_context_insight」这个**行为**，绝不断言配置的
    当前值：钉配置值会让用例随环境（.env）时红时绿，计划通则 3 明令禁止。
    """
    seen = {}

    def fake_agg(self, since="", days=14):
        seen["days"] = days
        return _usage_agg(days)

    monkeypatch.setattr("utils.agent_trace.store.TraceStore.list_runs",
                        lambda self, **kw: [])
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.aggregate_llm_usage", fake_agg)
    monkeypatch.setattr(osvc.Config, "OVERVIEW_TOKEN_DAYS", 9)
    out = osvc._load_context_insight({})
    assert out["range"]["days"] == 9
    assert seen["days"] == 9          # 配置值真的传下去了，不是 loader 里写死的 14


def test_load_context_insight_calls_without_token_usage_yields_none(monkeypatch):
    """有调用但全部缺 token_usage：分母为 0 -> 命中率 None（按调用数判会误报 0%）。"""
    agg = _usage_agg(14)
    agg.update({"llm_calls": 1, "input_tokens": 0, "cache_hit_ratio": 0.0,
                "cost_missing_calls": 1})
    _patch_usage(monkeypatch, agg)
    out = osvc._load_context_insight({})
    assert out["kpi"]["cache_hit_ratio"] is None
    assert out["kpi"]["cost"] is None


def test_load_context_insight_priced_range_keeps_real_values(monkeypatch):
    """已计价区间：命中率与费用照原样透传（防止 loader 过度置 None）。"""
    agg = _usage_agg(14)
    agg.update({"llm_calls": 3, "input_tokens": 1000, "output_tokens": 100,
                "total_tokens": 1100, "reasoning_tokens": 60, "cache_hit_ratio": 0.5,
                "cost_total": 0.25, "by_model": [("m", 1100)]})
    _patch_usage(monkeypatch, agg)
    out = osvc._load_context_insight({})
    assert out["kpi"]["cache_hit_ratio"] == 0.5
    assert out["kpi"]["cost"] == 0.25
    assert out["kpi"]["billed_tokens"] == 1100
    assert out["kpi"]["reasoning_tokens"] == 60
    assert out["by_model"] == [("m", 1100)]    # 二元组，不是 dict


def test_load_context_insight_free_calls_keep_zero_cost(monkeypatch):
    """billing=free（已计价为 0）：cost 必须是 0.0 —— 免费不能显示成未采集。"""
    agg = _usage_agg(14)
    agg.update({"llm_calls": 2, "input_tokens": 500, "output_tokens": 10,
                "total_tokens": 510, "cache_hit_ratio": 0.0, "cost_total": 0.0})
    _patch_usage(monkeypatch, agg)
    out = osvc._load_context_insight({})
    assert out["kpi"]["cost"] == 0.0
    assert out["kpi"]["cache_hit_ratio"] == 0.0


# ── 块 2：活跃度（复用按月聚合，键名 run_days） ──

def _patch_calendar(monkeypatch, run_days: dict):
    from datetime import date
    today = date.today()

    def fake(_store, year, month):
        if (year, month) == (today.year, today.month):
            return {"year": year, "month": month, "run_days": run_days}
        return {"year": year, "month": month, "run_days": {}}

    monkeypatch.setattr(
        "utils.agent_trace.calendar_query.query_trace_calendar", fake)


def test_load_activity_aggregates_run_days(monkeypatch):
    """按 run_days[日期] 的 run 列表求 total_tokens 之和，日期升序。"""
    _patch_calendar(monkeypatch, {
        "2026-10-16": [{"id": "r2", "total_tokens": 7}],
        "2026-10-15": [{"id": "r1", "total_tokens": 100},
                       {"id": "r0", "total_tokens": 50}],
    })
    out = osvc._load_activity({})
    assert out["days"] == [{"date": "2026-10-15", "tokens": 150},
                           {"date": "2026-10-16", "tokens": 7}]


def test_load_activity_missing_tokens_count_as_zero(monkeypatch):
    """run 字典缺 total_tokens 时按 0 计，不抛异常。"""
    _patch_calendar(monkeypatch, {"2026-10-16": [{"id": "r1"}]})
    out = osvc._load_activity({})
    assert out["days"] == [{"date": "2026-10-16", "tokens": 0}]


def test_load_activity_keeps_most_recent_56_days(monkeypatch):
    """喂 60 天时只保留**最近** 56 天（锁裁剪方向与首日，不只锁条数：改成 [:56] 必须红）。"""
    from datetime import date as _date, timedelta as _timedelta

    all_days = [(_date.today() - _timedelta(days=59 - i)).isoformat() for i in range(60)]
    by_month: dict = {}
    for d in all_days:
        by_month.setdefault(d[:7], {})[d] = [{"total_tokens": 1}]

    def fake(_store, year, month):
        return {"year": year, "month": month,
                "run_days": by_month.get("%04d-%02d" % (year, month), {})}

    monkeypatch.setattr(
        "utils.agent_trace.calendar_query.query_trace_calendar", fake)
    days = osvc._load_activity({})["days"]
    assert len(days) == 56
    assert days[0]["date"] == all_days[4]      # 最旧 4 天被裁掉
    assert days[-1]["date"] == all_days[-1]    # 保留到今天


def test_load_activity_degrades_on_calendar_error(monkeypatch):
    """某月聚合抛错时该月跳过，不把整块打挂。"""

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(
        "utils.agent_trace.calendar_query.query_trace_calendar", boom)
    assert osvc._load_activity({}) == {"days": []}


# ── 块 3：会话上下文档案 ──

class _Storage:
    def __init__(self, dialogs=None, run_map=None):
        self._dialogs = dialogs or []
        self._run_map = run_map or {}

    def list_dialogs(self, limit=100):
        return list(self._dialogs)

    def latest_trace_runs_for_dialogs(self, uuids):
        return {u: self._run_map[u] for u in uuids if u in self._run_map}


def _recent_iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_load_context_sessions_uses_dialog_run_map(monkeypatch):
    """会话标题来自 web_dialogs，run 映射来自 latest_trace_runs_for_dialogs。"""
    now = _recent_iso()
    storage = _Storage(
        dialogs=[{"dialog_uuid": "d1", "title": "茅台分析",
                  "current_mode": "react_stock",
                  "created_at": now, "updated_at": now}],
        run_map={"d1": "r1"})

    monkeypatch.setattr(osvc, "_store_totals",
                        lambda run_ids: {"r1": {"total_tokens": 100, "cost": 0.5,
                                               "reasoning_tokens": 66}})
    payload = osvc._load_context_sessions({"web_storage": storage}, days=7)
    card = payload["sessions"][0]
    assert card["dialog_uuid"] == "d1" and card["run_id"] == "r1"
    assert card["title"] == "茅台分析"
    assert card["agent_mode"] == "react_stock"
    assert card["billed_tokens"] == 100 and card["reasoning_tokens"] == 66
    assert card["cost"] == 0.5
    # 汇总 dict 里没有 output_tokens 键（老 / 局部形状）-> 卡面按未采集给 None，
    # 不能 .get(k, 0) 折成 0（那会让前端画出假 0 长度的回答段）。
    assert card["output_tokens"] is None
    assert payload["days"] == 7


def test_load_context_sessions_exposes_output_tokens(monkeypatch):
    """卡片新增 output_tokens（思考 / 回答 的分母）—— 这是**加键**，不是放宽既有断言。

    前端据此算 thinking = min(reasoning, output)、answer = output - thinking，
    保证 思考 + 回答 ≡ 输出（context_stats.py 铁律 2）；billed / cost / reasoning
    三量的既有语义一字不动（billed 仍是逐次求和、cost 仍是三态）。
    """
    now = _recent_iso()
    storage = _Storage(
        dialogs=[{"dialog_uuid": "d1", "title": "茅台分析",
                  "current_mode": "react_stock",
                  "created_at": now, "updated_at": now}],
        run_map={"d1": "r1"})
    monkeypatch.setattr(osvc, "_store_totals",
                        lambda run_ids: {"r1": {"total_tokens": 100, "cost": 0.5,
                                               "reasoning_tokens": 66,
                                               "output_tokens": 80}})
    card = osvc._load_context_sessions({"web_storage": storage}, days=7)["sessions"][0]
    assert card["output_tokens"] == 80
    assert card["billed_tokens"] == 100 and card["reasoning_tokens"] == 66
    assert card["cost"] == 0.5


def test_load_context_sessions_filters_out_stale_dialogs(monkeypatch):
    """窗口外的旧会话不进档案（按 updated_at / created_at 裁剪）。"""
    storage = _Storage(
        dialogs=[{"dialog_uuid": "old", "title": "旧会话", "current_mode": "react_stock",
                  "created_at": _recent_iso(30), "updated_at": _recent_iso(30)},
                 {"dialog_uuid": "new", "title": "新会话", "current_mode": "unified_plan",
                  "created_at": _recent_iso(1), "updated_at": _recent_iso(1)}],
        run_map={"new": "r9"})
    monkeypatch.setattr(osvc, "_store_totals",
                        lambda run_ids: {"r9": {"total_tokens": 5, "cost": None,
                                               "reasoning_tokens": 0}})
    payload = osvc._load_context_sessions({"web_storage": storage}, days=7)
    assert [c["dialog_uuid"] for c in payload["sessions"]] == ["new"]


def test_load_context_sessions_without_run_keeps_none(monkeypatch):
    """没有 run 映射的会话照常列出，token / 成本为 None（不是 0）。"""
    now = _recent_iso()
    storage = _Storage(
        dialogs=[{"dialog_uuid": "d2", "title": None, "current_mode": "plan",
                  "created_at": now, "updated_at": now}])
    monkeypatch.setattr(osvc, "_store_totals", lambda run_ids: {})
    card = osvc._load_context_sessions({"web_storage": storage})["sessions"][0]
    assert card["title"] == "未命名"
    assert card["run_id"] is None
    assert card["billed_tokens"] is None and card["reasoning_tokens"] is None
    assert card["cost"] is None
    # ⚠️ output_tokens 同样必须是 None（= 未采集）：前端只有判 null 才会「只画轨道 +
    # 标未采集」；写成 0 会被读成「回答 0 token」，正是铁律 3 禁止的假数据。
    assert card["output_tokens"] is None


def test_load_context_sessions_without_storage_returns_empty():
    """早退分支与成功路径返回**同形状**（都带 days），不留不对称。"""
    assert osvc._load_context_sessions({}) == {"sessions": [], "days": 7}
    # ⚠️ 还要锁 days 的**透传行为**（任务 11 复审登记的收紧项）：只按默认 7 断言的话，
    # 把 days 写死成 7 的实现照样绿 —— 显式传 3 必须回 3（7 / 3 都是形参默认与入参，
    # 不是配置项的值，不违反「不许 pin 可配置项」）。
    assert osvc._load_context_sessions({}, days=3) == {"sessions": [], "days": 3}


def test_load_context_sessions_degrades_when_list_dialogs_raises():
    """第三个返回点（list_dialogs 抛错）也要有形状断言 —— 任务 11 复审登记的收紧项。

    与另外两个返回点**同形状**：sessions 为空但 days 照传，绝不穿透到块级，
    也不返回 None（前端按 payload["sessions"] 取数，形状分叉会静默画空）。
    """
    class _Boom:
        def list_dialogs(self, limit=200):
            raise RuntimeError("db locked")

    assert osvc._load_context_sessions({"web_storage": _Boom()}, days=5) == {
        "sessions": [], "days": 5}


def test_load_context_sessions_degrades_when_totals_raise(monkeypatch):
    """_store_totals 抛错时降级为「照常列会话、token/成本未采集」，不穿透到块级。"""
    now = _recent_iso()
    storage = _Storage(dialogs=[{"dialog_uuid": "d1", "title": "会话甲",
                                 "current_mode": "plan",
                                 "created_at": now, "updated_at": now}],
                       run_map={"d1": "r1"})

    def boom(run_ids):
        raise RuntimeError("dirty step")

    monkeypatch.setattr(osvc, "_store_totals", boom)
    payload = osvc._load_context_sessions({"web_storage": storage}, days=7)
    card = payload["sessions"][0]
    assert card["run_id"] == "r1"
    assert card["billed_tokens"] is None and card["cost"] is None
    assert card["output_tokens"] is None      # 与 token / 成本同款降级：未采集，不是 0
    assert payload["days"] == 7


# ── _store_totals：llm_steps_totals 真实形状是逐 step 列表 ──

def _step(total=None, reasoning=None, cost=None, output=None):
    extra = {}
    if total is not None or reasoning is not None or output is not None:
        extra["token_usage"] = {"total_tokens": total or 0,
                                "reasoning_tokens": reasoning or 0,
                                "output_tokens": output or 0}
    if cost is not None:
        extra["cost"] = {"total": cost, "billing": "paid"}
    else:
        extra["cost"] = {"total": None, "billing": "unpriced"}
    return {"step_name": "TokenCompatibleChatOpenAI", "extra": json.dumps(extra)}


def test_store_totals_aggregates_real_llm_steps_shape(monkeypatch):
    """真实返回是 {run_id: [{"step_name","extra"}]} —— 必须逐 step 解析 extra。"""
    steps = {"r1": [_step(total=100, reasoning=60, cost=0.5),
                    _step(total=20, reasoning=6)]}
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: steps)
    out = osvc._store_totals(["r1"])
    assert out["r1"]["total_tokens"] == 120
    assert out["r1"]["reasoning_tokens"] == 66
    assert out["r1"]["cost"] == 0.5


def test_store_totals_aggregates_output_tokens(monkeypatch):
    """output_tokens 必须一起汇总：它与 total / reasoning 同源（同一个 usage dict、
    同一个 normalize_token_usage_dict 归一入口），是「思考 + 回答」的**分母**。

    漏掉它，会话卡就只能退回按计费求比例的错分母，或把缺失画成 0（铁律 3）。
    """
    steps = {"r1": [_step(total=100, reasoning=60, output=40, cost=0.5),
                    _step(total=20, reasoning=6, output=14)]}
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: steps)
    out = osvc._store_totals(["r1"])["r1"]
    assert out["output_tokens"] == 54
    # 三量互不干扰（同一份 usage dict 里各取各的）
    assert out["total_tokens"] == 120 and out["reasoning_tokens"] == 66
    assert out["cost"] == 0.5


def test_store_totals_normalizes_usage_without_total_tokens(monkeypatch):
    """只有 input_tokens/output_tokens、没有 total_tokens 的 step（任务 2 踩过的同一形态）。

    ⚠️ 不能直读 extra.token_usage.total_tokens：缺该键时直读会静默得 0，再累计进
    大屏 KPI。必须走仓库唯一归一入口 utils.token_usage.normalize_token_usage_dict
    —— 它缺 total 时回退 input + output。
    """
    step = {"step_name": "TokenCompatibleChatOpenAI",
            "extra": json.dumps({"token_usage": {"input_tokens": 300,
                                                 "output_tokens": 40}})}
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: {"r1": [step]})
    out = osvc._store_totals(["r1"])
    assert out["r1"]["total_tokens"] == 340     # 回退 input + output，不是静默 0


def test_store_totals_unpriced_only_cost_is_none(monkeypatch):
    """一次都没记过价（cost.total 全 None）-> cost 为 None，不是 0（铁律 3）。"""
    steps = {"r1": [_step(total=5, reasoning=1)]}
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: steps)
    assert osvc._store_totals(["r1"])["r1"]["cost"] is None


def test_store_totals_missing_cost_key_is_none(monkeypatch):
    """extra 里**根本没有 cost 键**（从未记录，COST_TRACKING_ENABLED=false 的历史行形态）
    -> cost 为 None，不是 0.0（铁律 3「免费 ≠ 0 元」；任务 7 那次事故的同族分支）。"""
    step = {"step_name": "TokenCompatibleChatOpenAI",
            "extra": json.dumps({"token_usage": {"total_tokens": 9,
                                                 "reasoning_tokens": 3}})}
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: {"r1": [step]})
    out = osvc._store_totals(["r1"])["r1"]
    assert out["cost"] is None
    assert out["total_tokens"] == 9 and out["reasoning_tokens"] == 3


def test_store_totals_skips_cache_hit_pseudo_steps(monkeypatch):
    """cache_hit 伪步骤（A8 缓存命中，非真实调用）必须跳过 —— 与
    store.aggregate_llm_usage 的 extra.cache_hit 过滤同口径，否则三个入口结论分叉。"""
    pseudo = {"step_name": "TokenCompatibleChatOpenAI",
              "extra": json.dumps({"cache_hit": True,
                                   "token_usage": {"total_tokens": 999,
                                                   "input_tokens": 900,
                                                   "output_tokens": 99},
                                   "cost": {"total": 1.5}})}
    real = _step(total=10, reasoning=2, cost=0.25)
    monkeypatch.setattr("utils.agent_trace.store.TraceStore.llm_steps_totals",
                        lambda self, run_ids: {"r1": [pseudo, real]})
    out = osvc._store_totals(["r1"])["r1"]
    assert out["total_tokens"] == 10 and out["reasoning_tokens"] == 2
    assert out["cost"] == 0.25


def _raw_step(extra_value):
    return {"step_name": "TokenCompatibleChatOpenAI", "extra": extra_value}


def test_store_totals_tolerates_extra_list(monkeypatch):
    """extra 是 JSON 列表（非 dict）-> 跳过该步，不抛（合成输入钉住真实不可达形态）。"""
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.llm_steps_totals",
        lambda self, run_ids: {"r1": [_raw_step(json.dumps([1, 2])),
                                      _step(total=7, cost=0.1)]})
    out = osvc._store_totals(["r1"])["r1"]
    assert out["total_tokens"] == 7 and out["cost"] == 0.1


def test_store_totals_tolerates_non_dict_token_usage(monkeypatch):
    """token_usage 非 dict（脏值）-> 该步 token 按未采集记 0，不抛；同一步成本仍照计。"""
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.llm_steps_totals",
        lambda self, run_ids: {"r1": [
            _raw_step(json.dumps({"token_usage": 5, "cost": {"total": 0.3}}))]})
    out = osvc._store_totals(["r1"])["r1"]
    assert out["total_tokens"] == 0 and out["reasoning_tokens"] == 0
    assert out["cost"] == 0.3


def test_store_totals_tolerates_non_numeric_cost(monkeypatch):
    """cost.total 非数值（脏字符串）-> 按未计价给 None（不抛、也不按 0 计入金额）。"""
    monkeypatch.setattr(
        "utils.agent_trace.store.TraceStore.llm_steps_totals",
        lambda self, run_ids: {"r1": [
            _raw_step(json.dumps({"token_usage": {"total_tokens": 4},
                                  "cost": {"total": "N/A"}}))]})
    out = osvc._store_totals(["r1"])["r1"]
    assert out["total_tokens"] == 4
    assert out["cost"] is None


def test_store_totals_empty_run_ids_returns_empty():
    assert osvc._store_totals([]) == {}


# ── 接线：三个块必须真注册（只有 loader 没注册 = 死代码） ──

def test_blocks_register_three_context_blocks():
    by_id = {b.id: b for b in osvc.BLOCKS}
    assert by_id["usage"].title == "上下文洞察"
    assert by_id["usage"].loader is osvc._load_context_insight
    assert by_id["activity"].loader is osvc._load_activity
    assert by_id["activity"].title == "活跃度"
    assert by_id["sessions"].loader is osvc._load_context_sessions
    assert by_id["sessions"].title == "会话上下文档案"
    # ⚠️ 字面量断言：用 osvc._SLOW_BLOCK_TIMEOUT_S 自比的话，同改常量与 Block 参数仍会绿。
    # 20.0 锁「sessions 用了慢块档」这一契约（Block 默认档是 15.0）。
    assert by_id["sessions"].timeout == 20.0


def test_blocks_registry_has_no_duplicate_ids():
    ids = [b.id for b in osvc.BLOCKS]
    assert len(ids) == len(set(ids))

def setup_function(_):
    # 本文件的连接释放用例会真走 _load_block 的缓存 / 旧缓存降级：先清账 ——
    # 否则上一个用例留下的缓存会让「loader 抛错」变成「回退旧缓存 ok=True」（那是
    # 另一条既有用例的语义），断言就变成了在测别的东西。
    osvc._run_cache.clear()
    osvc._run_locks.clear()


# ── _load_block 的 worker 线程收尾：释放该线程的 store 连接缓存（任务 13） ──
#
# 为什么值得单测：这修的是**无界内存增长**（实测 5 轮强刷 RSS 59.7MB→832.7MB、连接 0→25→40），
# 根因是 store 的连接缓存**线程局部**（_MAX_CONNS_PER_THREAD 是每线程 LRU）—— 线程跑完即死，
# 那批连接既不被 LRU 淘汰、也不随线程关闭。断言一律基于 store 的**真实结构**
# （_all_conns / _thread_cache），不另造影子计数器。

def _store_mod():
    """返回 **模块**（不是 TraceStore 实例）：_all_conns 是模块级列表（store.py:38）。"""
    from utils.agent_trace import store as store_mod
    return store_mod


def _patch_month_path(monkeypatch, tmp_path):
    """把月库路径指到 tmp_path → 连接与建表全落临时目录，绝不碰 db/agent_trace/。"""
    st = _store_mod()
    monkeypatch.setattr(st.store, "month_db_path",
                        lambda month: str(tmp_path / f"agent_trace_{month}.db"))
    return st


def _conn_block(seen, boom=False, timeout=5.0, hold=None):
    """块 loader：在 **worker 线程里**真开一条 store 连接（走真实缓存路径）。"""
    def loader(ctx):
        st = _store_mod()
        st.store.conn_for_month("202601", create=True)
        seen["cache_in_thread"] = len(st.store._thread_cache())
        seen["all_in_thread"] = len(st._all_conns)
        if hold is not None:
            hold.wait(5)
        if boom:
            raise RuntimeError("loader boom")
        return {"ok": True}
    return osvc.Block("connblk", "连接块", "center", "research", 0, 0, loader, timeout)


def test_load_block_releases_thread_conns_on_success(tmp_path, monkeypatch):
    """正常路径：块返回后，worker 线程开的那条连接不得残留（_all_conns 归零）。"""
    st = _patch_month_path(monkeypatch, tmp_path)
    before = len(st._all_conns)
    seen = {}
    res = osvc._load_block(_conn_block(seen), {}, force=True)
    assert res["ok"] is True
    # 先证明「真的开了连接」，否则下面的 delta == 0 可能是恒真
    assert seen["cache_in_thread"] >= 1
    assert seen["all_in_thread"] == before + 1
    assert len(st._all_conns) == before


def test_load_block_releases_thread_conns_on_loader_error(tmp_path, monkeypatch):
    """异常路径：loader 抛错时 finally 照样释放（否则失败块会稳定泄漏）。"""
    st = _patch_month_path(monkeypatch, tmp_path)
    before = len(st._all_conns)
    seen = {}
    res = osvc._load_block(_conn_block(seen, boom=True), {}, force=True)
    assert res["ok"] is False and "loader boom" in res["error"]
    assert seen["cache_in_thread"] >= 1        # 异常路径也真开过连接
    assert len(st._all_conns) == before


def test_load_block_without_release_leaks_thread_conns(tmp_path, monkeypatch):
    """反向断言：把释放打成 no-op → 连接**必须**残留（证明上面两条不是恒真）。"""
    st = _patch_month_path(monkeypatch, tmp_path)
    monkeypatch.setattr(osvc, "_release_thread_conns", lambda: None)
    before = len(st._all_conns)
    seen = {}
    assert osvc._load_block(_conn_block(seen), {}, force=True)["ok"] is True
    leaked = list(st._all_conns[before:])
    assert len(leaked) == 1
    # 手工清账：这条是**故意**复现泄漏，别把连接留给后面的用例
    for conn in leaked:
        conn.close()
    st._all_conns[:] = st._all_conns[:before]


def test_load_block_keeps_conn_until_worker_returns_after_timeout(tmp_path, monkeypatch):
    """措辞精度：块**返回**后归零，但 worker 未返回前它持有的连接**还没**释放。

    超时只是调用方放弃等待，不是 worker 结束 —— finally 在 worker 线程内执行，
    所以释放发生在 worker 收尾那一刻（超时后仍在跑的块会短暂占着连接，这不是泄漏）。
    """
    st = _patch_month_path(monkeypatch, tmp_path)
    before = len(st._all_conns)
    hold = threading.Event()
    seen = {}
    res = osvc._load_block(_conn_block(seen, timeout=0.2, hold=hold), {}, force=True)
    assert res["ok"] is False and "超时" in res["error"]
    assert len(st._all_conns) == before + 1    # worker 还在跑 → 仍持有
    hold.set()
    deadline = time.time() + 5
    while len(st._all_conns) > before and time.time() < deadline:
        time.sleep(0.02)
    assert len(st._all_conns) == before        # worker 收尾后归零

