"""取数层：数据槽 → 工具降级链 → 结论卡（Evidence Card）。

设计：docs/arch/2026-10-31-long-report-agent-outline-design-m2.md §2
口径：
- 每个槽按链依次尝试；工具空/失败/解析不出 → 下一跳；
- 全链失败 → status=no_data + gaps，**不填 0、不编数**；
- 结论卡落 `evidence/<ev>.json`，写作节只吃「摘要行 + ev_id」。

M3 追加「多期序列」：需要走势的槽（`SERIES_SLOTS`）先按原链拿单期结论卡，再补一次
**逐期问法**的取数，经 `series.parse_wide_series` 转置成带 `period` 的 items。
补不到（工具不可用 / 问法拿不到宽表 / 期数不足）一律**原样返回单期卡片**，
不新增 gaps、不抛异常 —— 即「新逻辑失败 = 退回原有的单期行为」。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.deep_report.series import parse_wide_series
from agents.subagent.executor import SubagentExecutor
from agents.subagent.models import (STATUS_OK, STATUS_TIMEOUT, STOP_TIMEOUT,
                                    SubagentResult, SubagentTask)

logger = logging.getLogger(__name__)

# 子代理整体看门狗：deadline 只在回合之间检查，单次 LLM/工具调用可能挂住
_EXEC_TIMEOUT_PAD_S = 30.0
_DERIVE_TIMEOUT_S = 30.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 结论卡 schema（子代理结构化出口的契约）
CARD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "asof": {"type": "string", "description": "数据日期 YYYY-MM-DD"},
        "status": {"type": "string", "enum": ["ok", "no_data"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": ["number", "string"]},
                    "unit": {"type": "string"},
                    "source": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["metric", "value", "source"],
            },
        },
        # 图表走 spec（LLM 只给数据与类型，ECharts option 由编译器生成）
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string",
                             "enum": ["line", "bar", "stacked", "combo", "heatmap",
                                      "pie", "histogram", "scatter", "area"]},
                    "title": {"type": "string"},
                    "x": {"type": "object"},
                    "series": {"type": "array"},
                    "x_labels": {"type": "array"},
                    "y_labels": {"type": "array"},
                    "data": {"type": "array"},
                    "unit": {"type": "object"},
                    "source": {"type": "string"},
                    "asof": {"type": "string"},
                },
                "required": ["id", "type", "title", "series", "source", "asof"],
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["asof", "status", "items"],
}

# 槽 → [(工具名, 目标模板)]，顺序即降级顺序
SLOT_CHAINS: Dict[str, List[Tuple[str, str]]] = {
    "snapshot": [
        ("mx_data_query", "查询{universe}当前点位/价格、涨跌幅、成交额、估值分位。"),
        ("iwc_query", "查询{universe}指数或标的最新点位、涨跌幅、成交额。"),
    ],
    "valuation": [
        ("mx_data_query", "查询{universe}市盈率、市净率、股息率及历史分位（板块取中位数）。"),
        ("iwc_query", "查询{universe}的估值指标与历史分位。"),
    ],
    "capital_flow": [
        ("mx_data_query", "查询{universe}主力净流入（5日/20日）、北向资金、两融余额。"),
        ("iwc_query", "查询{universe}主力资金净流入、融资融券余额。"),
    ],
    "constituents": [
        ("mx_data_query", "查询{universe}成分股/主要标的的涨跌幅、成交额、PB、ROE 横截面（取前 10）。"),
        ("iwc_query", "查询{universe}成分股涨跌幅、市净率排序（前 10）。"),
    ],
    "consensus_poll": [
        ("iwc_search", "检索近 90 天关于{universe}的卖方研报，汇总机构评级分布与目标价区间。"),
        ("mx_search_news", "检索近 90 天{universe}相关机构观点与评级。"),
    ],
    "industry_space": [
        ("iwc_search", "检索{universe}行业规模、增速的外部测算（须注明机构与年份）。"),
        ("web_search", "搜索{universe}行业规模与增速的最新权威数据（注明来源与年份）。"),
    ],
    "share_cr": [
        ("iwc_search", "检索{universe}市场份额与集中度（CR3/CR5）数据（注明机构与年份）。"),
        ("web_search", "搜索{universe}市场份额、集中度数据（注明来源与年份）。"),
    ],
}

# ── 多期序列（M3）：需要「走势」的槽 → (metric_hint, 逐期问法模板) ──
# 问法与返回形态都经 `scripts/devtools/probe_series_feasibility.py` 实测
# （2026-11-01，iwc_query）：
#   · 「近60个交易日的收盘点位和成交额」→ 宽表 60 期「收盘价」（另含 60 期成交额），
#     列名形如 `收盘价[20261030]`，行列转置后正好是折线的 x/y；
#   · 「近12个季度的市净率」→ 宽表 12 期「市净率」（季度末日期）；
#   · ⚠️ 「近20个交易日的成交额」（不带「每日」）返回的是 `区间成交额[起-止]`
#     **区间合计单值**，转置不出序列 —— 所以模板里一律用「每日/收盘点位」这类逐期问法。
# 拿不到就退回单期卡片，不写 gaps（缺序列≠缺数据）。
SERIES_SLOTS: Dict[str, Tuple[str, str]] = {
    "snapshot": ("收盘价", "{universe}近60个交易日的收盘点位和成交额"),
    "valuation": ("市净率", "{universe}近12个季度的市净率"),
}
SERIES_MIN_POINTS = 4          # 与 chart_candidates._MIN_SERIES_POINTS 对齐
SERIES_MAX_POINTS = 120        # 卡片里最多留最近 120 期（60 期日线够画，防 payload 爆炸）
SERIES_TIMEOUT_S = 20.0        # 同步工具调用上限：超时就退回单期，不拖死整个槽

_SERIES_GOAL = (
    "\n\n多期序列（有就给，没有就只给单期）：若该指标能取到历史序列"
    "（如近 60 个交易日点位、近 12 个季度估值），请在 items 里**逐期**列出"
    "（同一 metric 多条，period 填 YYYY-MM-DD），不要只给最新值；"
    "取不到序列时按原口径只给单期，禁止编数、禁止用单期值冒充序列。"
)


def _norm_items(raw: Dict[str, Any], dropped: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    bad = 0
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            bad += 1
            continue
        metric = str(it.get("metric") or "").strip()
        source = str(it.get("source") or "").strip()
        value = it.get("value")
        if not metric or not source or value in (None, ""):
            bad += 1
            continue
        # NaN / ±inf 是脏数据（JSON 里会变成 null 或 "NaN"），不能当数字写进报告
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            bad += 1
            continue
        out.append({"metric": metric, "value": value, "unit": str(it.get("unit") or ""),
                    "source": source, "period": str(it.get("period") or "")})
    if bad and dropped is not None:
        # 静默丢弃会让「证据覆盖」虚高：丢弃必须有迹可循（评审 P2）
        dropped.append(f"丢弃 {bad} 条无指标/无来源/空值的 items")
    return out


_RECT_TYPES = {"line", "bar", "stacked", "combo", "area"}


def _spec_ok(spec: Dict[str, Any]) -> bool:
    """形状校验：类型必须已知；直角系图 x 与每条 series 必须等长（否则画出来是一坨竖点）。"""
    ctype = str(spec.get("type") or "").lower()
    known = _RECT_TYPES | {"pie", "histogram", "scatter", "heatmap"}
    if ctype not in known:
        return False
    if ctype in _RECT_TYPES:
        xs = (spec.get("x") or {}).get("values") or []
        series = spec.get("series") or []
        if not xs or not series:
            return False
        for s in series:
            data = s.get("data") or []
            if len(data) != len(xs):
                return False
    elif ctype in ("pie", "histogram", "scatter"):
        if not (spec.get("series") or spec.get("data")):
            return False
    elif ctype == "heatmap":
        if not (spec.get("data") and spec.get("x_labels") and spec.get("y_labels")):
            return False
    return True


def _norm_charts(raw: Dict[str, Any], dropped: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """只保留「有 source 且 asof 合法、形状正确」的 spec —— 无源/畸形图不得出现。"""
    out: List[Dict[str, Any]] = []
    bad = 0
    for spec in raw.get("charts") or []:
        if not isinstance(spec, dict):
            bad += 1
            continue
        if not str(spec.get("source") or "").strip():
            bad += 1
            continue
        if not _DATE_RE.match(str(spec.get("asof") or "").strip()):
            bad += 1
            continue
        if not _spec_ok(spec):
            bad += 1
            continue
        out.append(spec)
    if bad and dropped is not None:
        dropped.append(f"丢弃 {bad} 张无来源/asof 非法/形状不符的图表")
    return out


def _fallback_chart(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """规则兜底：取**同一单位**下最多的那组数值出一张对比柱图（零 LLM 成本）。

    不同单位（点位 / 成交额 / 倍）不能共用一个轴，宁可不出图也不出误导读者的图。
    """
    groups: Dict[str, List[tuple]] = {}
    for it in card.get("items") or []:
        try:
            f = float(it.get("value"))
        except (TypeError, ValueError):
            continue
        unit = str(it.get("unit") or "")
        groups.setdefault(unit, []).append((str(it.get("metric") or "")[:16], f))
    if not groups:
        return None
    unit, nums = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(nums) < 2:
        return None
    nums = nums[:6]
    return {
        "id": f"auto_{card.get('ev_id')}",
        "type": "bar",
        "title": f"{card.get('slot')} 关键数值（自动生成）",
        "x": {"values": [n[0] for n in nums]},
        "series": [{"name": unit or "数值", "data": [n[1] for n in nums]}],
        "unit": {"y1": unit},
        "source": "结论卡汇总（规则兜底）",
        "asof": card.get("asof"),
    }


def validate_card(raw: Dict[str, Any]) -> Tuple[bool, str]:
    """结论卡校验：无源数字、空 items、asof 非法、status 自相矛盾 → 判 no_data。"""
    if not isinstance(raw, dict):
        return False, "结论卡不是对象"
    asof = str(raw.get("asof") or "").strip()
    if not _DATE_RE.match(asof):
        return False, f"asof 非法: {asof!r}（必须是 YYYY-MM-DD）"
    status = str(raw.get("status") or "").strip().lower()
    if status and status not in ("ok", "no_data", "partial"):
        return False, f"status 非法: {status!r}"
    items = _norm_items(raw)
    if status == "no_data" and items:
        return False, "status=no_data 却带 items（自相矛盾）"
    if not items:
        return False, "items 为空（无可用数字）"
    return True, ""


# ── 序列取数的执行细节（纯工具函数，便于单测） ──
def _as_text(out: Any) -> Optional[str]:
    """工具返回值统一成文本（多数技能工具返回 JSON 字符串）。"""
    if out is None:
        return None
    if isinstance(out, str):
        return out
    try:
        return json.dumps(out, ensure_ascii=False, default=str)
    except (TypeError, ValueError):     # pragma: no cover - 极少数不可序列化对象
        return str(out)


def invoke_tool(fn: Any, query: str) -> Optional[str]:
    """调一个技能工具：LangChain 工具（`.invoke(dict)`）或普通函数都支持。

    参数面按「query(+limit)」逐档退让：`iwc_query` 有 `limit`（宽表要放大到 80
    才够 60 期），`mx_data_query` 只有 `query`，多传参数会被 pydantic 拒掉。
    """
    invoke = getattr(fn, "invoke", None)
    if callable(invoke):
        for args in ({"query": query, "limit": "80"}, {"query": query}, {"q": query}):
            try:
                return _as_text(invoke(args))
            except Exception:      # noqa: BLE001 —— 参数不合就换下一档，全失败返回 None
                continue
        return None
    if callable(fn):
        for call in (lambda: fn(query), lambda: fn(query=query),
                     lambda: fn(query, limit="80")):
            try:
                return _as_text(call())
            except Exception:      # noqa: BLE001
                continue
    return None


def call_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """跑 fn 但最多等 `timeout_s` 秒（同步网络工具不能拖死整个数据槽）。

    用 daemon 线程而不是 `ThreadPoolExecutor`：后者是非 daemon 的，超时后遗留线程
    会在解释器退出时被 join，等于把「超时」变成「卡住退出」。
    """
    box: Dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:     # noqa: BLE001 —— 取数失败是数据缺口，不是崩溃
            box["error"] = e

    th = threading.Thread(target=runner, name="deep-series", daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        raise TimeoutError(f"序列取数超时（>{timeout_s:.0f}s）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def series_line_spec(series: List[Dict[str, Any]], *, ev_id: str,
                     asof: str) -> Dict[str, Any]:
    """多期 items → 折线 spec（形状口径同 `_fallback_chart`：自带 source + asof）。"""
    metric = str(series[0].get("metric") or "")
    periods = [str(it.get("period") or "") for it in series]
    return {
        "id": f"ser_{ev_id}",
        "type": "line",
        "title": f"{metric}走势（{periods[0]}~{periods[-1]}）",
        "x": {"values": periods},
        "series": [{"name": metric, "data": [it.get("value") for it in series]}],
        "unit": {"y1": str(series[0].get("unit") or "")},
        "source": str(series[0].get("source") or "iwc_query"),
        "asof": asof if _DATE_RE.match(asof or "") else periods[-1],
    }


class FetchService:
    """按槽取数：跑子代理 → 校验 → 落盘。

    `series_fetcher`（可选）用于**离线注入**序列取数器：签名为
    `(tool_name, query) -> payload_text`。缺省时从执行器已装载的同名工具取数
    （与子代理用的是同一批工具，不新增数据源）。
    """

    def __init__(self, *, executor: Optional[SubagentExecutor], store, rid: str, log=None,
                 series_fetcher: Optional[Callable[[str, str], Optional[str]]] = None,
                 series_enabled: bool = True, derive_fetcher=None,
                 consensus_fetcher=None):
        self.executor = executor
        self.store = store
        self.rid = rid
        self.log = log or logger
        self.series_fetcher = series_fetcher
        self.series_enabled = series_enabled
        self._seq = 0
        self._lock = threading.Lock()
        if derive_fetcher is None:
            def _default_derive(board_name: str):
                from tools.fetcher import ak_board_industry_cons
                return ak_board_industry_cons(board_name)
            self.derive_fetcher = _default_derive
        else:
            self.derive_fetcher = derive_fetcher
        if consensus_fetcher is None:
            def _default_consensus(keyword: str):
                from tools.fetcher.consensus_src import fetch_research_consensus
                return fetch_research_consensus(keyword)
            self.consensus_fetcher = _default_consensus
        else:
            self.consensus_fetcher = consensus_fetcher

    def _next_ev(self) -> str:
        with self._lock:   # 并发取数时 ev 编号必须单调且不撞
            self._seq += 1
            return f"ev_{self._seq:03d}"

    # ── 多期序列（M3） ──
    def _series_payload(self, tool: str, query: str) -> Optional[str]:
        """取一次宽表 payload：注入器优先（单测离线），否则用执行器上的同名工具。"""
        if self.series_fetcher is not None:
            return call_with_timeout(lambda: self.series_fetcher(tool, query),
                                     SERIES_TIMEOUT_S)
        tools = getattr(self.executor, "tools", None)
        if not isinstance(tools, Mapping):
            return None
        fn = tools.get(tool)
        if fn is None:
            return None
        return call_with_timeout(lambda: invoke_tool(fn, query), SERIES_TIMEOUT_S)

    def _attach_series(self, slot: str, universe: str, tool: str,
                       items: List[Dict[str, Any]], charts: List[Dict[str, Any]],
                       *, ev_id: str, asof: str
                       ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """给需要走势的槽补多期 items（任何失败都原样返回单期卡片）。"""
        if not self.series_enabled or slot not in SERIES_SLOTS:
            return items, charts
        metric_hint, query_tpl = SERIES_SLOTS[slot]
        query = query_tpl.format(universe=universe)
        stats: Dict[str, Any] = {}
        try:
            payload = self._series_payload(tool, query)
            if not payload:
                self.log.info("序列无返回，保留单期 slot=%s tool=%s", slot, tool)
                return items, charts
            series = parse_wide_series(payload, metric_hint, source=tool, stats=stats)
        except Exception:              # noqa: BLE001 —— 补序列失败绝不能拖垮取数
            self.log.warning("序列取数失败，保留单期 slot=%s tool=%s", slot, tool,
                             exc_info=True)
            return items, charts

        series = series[-SERIES_MAX_POINTS:]
        if len(series) < SERIES_MIN_POINTS:
            self.log.info("序列期数不足，保留单期 slot=%s tool=%s 期数=%d stats=%s",
                          slot, tool, len(series), stats)
            return items, charts

        items = list(items) + self._anchor_item(items, series) + series
        if not charts:
            charts = [series_line_spec(series, ev_id=ev_id, asof=asof)]
        self.log.info("序列已补 slot=%s tool=%s 指标=%s 期数=%d 首末=%s~%s",
                      slot, tool, series[0].get("metric"), len(series),
                      series[0].get("period"), series[-1].get("period"))
        return items, charts

    @staticmethod
    def _anchor_item(existing: List[Dict[str, Any]],
                     series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """补一条「当期值」锚点（period 留空）。

        为什么需要：`summary_lines` 只取前 N 条、`qa.card_metrics` 只比对**第一条**，
        两者都默认 items 里第一条是「当期值」。序列按日期升序时第一条是最旧的，
        正文说「当前收盘价 7962」会被判成与证据不符 —— 锚点把当期值放回第一位。
        带 period 的序列点不受影响（chart_candidates 用 period 分组，空 period 自动跳过）。
        """
        metric = str(series[-1].get("metric") or "")
        for it in existing:
            if str(it.get("metric") or "") == metric:
                return []          # 单期卡片已给过同指标 → 不重复
        last = series[-1]
        return [{"metric": metric, "value": last.get("value"),
                 "unit": str(last.get("unit") or ""),
                 "source": str(last.get("source") or ""), "period": ""}]

    def _run_executor(self, task: SubagentTask, *, cancel=None, deadline_s: float = 120.0):
        """带**硬超时**地跑子代理：deadline 只在回合间检查，单次调用可能挂住。"""
        try:
            return call_with_timeout(lambda: self.executor.run(task, cancel=cancel),
                                     float(deadline_s) + _EXEC_TIMEOUT_PAD_S)
        except TimeoutError:
            logger.warning("子代理硬超时 slot=%s (>%.0fs)", task.slot,
                           float(deadline_s) + _EXEC_TIMEOUT_PAD_S)
            return SubagentResult(STATUS_TIMEOUT, STOP_TIMEOUT,
                                  diagnostic=f"整体超时（>{deadline_s + _EXEC_TIMEOUT_PAD_S:.0f}s）")

    def fetch(self, slot: str, universe: str, *, cancel=None, deadline_s: float = 120.0) -> Dict[str, Any]:
        ev_id = self._next_ev()
        chain = SLOT_CHAINS.get(slot) or []
        gaps: List[str] = []
        if not chain:
            card = self._card(ev_id, slot, date.today().isoformat(), "no_data",
                              [], [f"未配置取数链：{slot}"], "low")
            self._persist(card)
            return card
        if self.executor is None:
            card = self._card(ev_id, slot, date.today().isoformat(), "no_data",
                              [], ["未注入子代理执行器（M2 fetch 未启用）"], "low")
            self._persist(card)
            return card

        for tool, goal_tpl in chain:
            goal = (goal_tpl.format(universe=universe)
                    + "\n\n输出结论卡时：若数据支持，请在 charts 里给 1–3 张图"
                      "（同一单位才能放同一张；每张图必须带 source 与 asof；"
                      "多期数据用 line，横向对比用 bar）。"
                    + (_SERIES_GOAL if slot in SERIES_SLOTS else ""))
            task = SubagentTask(label=f"{slot}:{tool}", slot=slot, goal=goal,
                                output_schema=CARD_SCHEMA, tool_whitelist=(tool,),
                                max_turns=8, deadline_s=deadline_s)
            result = self._run_executor(task, cancel=cancel, deadline_s=deadline_s)
            if result.status != STATUS_OK or not result.structured:
                gaps.append(f"{tool}: {result.status}")
                continue
            ok, reason = validate_card(result.structured)
            if not ok:
                gaps.append(f"{tool}: {reason}")
                continue
            asof = str(result.structured.get("asof"))
            card_gaps = list(result.structured.get("gaps") or [])
            items = _norm_items(result.structured, dropped=card_gaps)
            charts = _norm_charts(result.structured, dropped=gaps)
            items, charts = self._attach_series(slot, universe, tool, items, charts,
                                                ev_id=ev_id, asof=asof)
            if not charts:
                auto = _fallback_chart({"ev_id": ev_id, "slot": slot,
                                        "asof": asof, "items": items})
                charts = [auto] if auto else []
            card = self._card(ev_id, slot, asof,
                              "ok", items, card_gaps,
                              str(result.structured.get("confidence") or "medium"),
                              tool_calls=result.tool_calls, turns=result.turns,
                              charts=charts)
            self._persist(card)
            return card

        card = self._card(ev_id, slot, date.today().isoformat(), "no_data",
                          [], gaps or ["全链无数据"], "low")
        self._persist(card)
        return card

    def fetch_capability(self, capability: str, universe: str, *, cancel=None,
                         symbol: str = "", candidates: Optional[List[Dict[str, Any]]] = None,
                         deadline_s: float = 120.0,
                         tiers: Optional[Dict[str, List[Dict[str, str]]]] = None) -> Dict[str, Any]:
        """按**能力**取数（Step 2）：子代理拿到该能力对应的技能工具，自己决定调哪个。

        与 `fetch(slot)`（旧厂商链）并存：能力路径失败由调用方决定是否回落旧链。
        `tiers`：模板声明的价格带分组（peer_financials / peer_series 用）。
        """
        from agents.deep_report.capabilities import get as cap_get

        ev_id = self._next_ev()
        try:
            spec = cap_get(capability)
        except KeyError:
            card = self._card(ev_id, capability, date.today().isoformat(), "no_data",
                              [], [f"未知能力：{capability}"], "low")
            self._persist(card)
            return card

        # 逐股类能力由程序直接取数（子代理只跑几轮，拿不全 10 只股票的财报）
        if capability in ("peer_financials", "peer_series"):
            derived = self._derive_peer(ev_id, capability, universe, tiers)
            if derived is not None:
                self._persist(derived)
                return derived
            card = self._card(ev_id, capability, date.today().isoformat(), "no_data",
                              [], [f"{capability}: 模板未声明 stock_tiers 或数据未取到"], "low")
            card["capability"] = capability
            self._persist(card)
            return card

        tools = tuple(spec.get("agent_tools") or ())
        if self.executor is None or not tools:
            card = self._card(ev_id, capability, date.today().isoformat(), "no_data",
                              [], ["未注入子代理执行器或无可用技能工具"], "low")
            self._persist(card)
            return card

        target = symbol or universe
        goal_parts = [
            f"【能力】{spec.get('name')}：{spec.get('desc')}",
            f"标的：{target}（用户原话：{universe}）",
            "技能工具通常需要证券代码；请优先使用给定代码调用工具。",
        ]
        if spec.get("notes"):
            goal_parts.append(f"工具选择提示：{spec['notes']}")
        if capability == "constituents":
            goal_parts.append(f"板块名称：{universe}（get_board_stocks 请传板块名称，不要传指数代码）")
        if candidates:
            cand_text = "；".join(f"{c.get('code')} {c.get('name')}（{c.get('kind')}）"
                                 for c in candidates[:5])
            goal_parts.append(
                f"名称可能对应多个标的，候选（按字面匹配度排序，未必对）：{cand_text}。"
                "请按行业语义选最匹配的一个调用工具；若都不合适，改用问句类工具按名称检索。")
        goal_parts += [
            "请调用可用工具取真实数据；若该数据是**序列**（历史点位/成交额/估值），"
            "在 items 里**逐期**列出并填 period=日期（YYYY-MM-DD），不要只给一个总数。",
            "取不到就返回 status=no_data 并在 gaps 说明，禁止编造或用 0 代替。",
            "输出结论卡时，若数据支持再给 charts（同一单位才能放同一张，每张带 source 与 asof）。",
        ]
        goal = "\n".join(goal_parts)
        task = SubagentTask(label=f"cap:{capability}", slot=capability, goal=goal,
                            output_schema=CARD_SCHEMA, tool_whitelist=tools,
                            max_turns=8, deadline_s=deadline_s)
        result = self._run_executor(task, cancel=cancel, deadline_s=deadline_s)
        if result.status != STATUS_OK or not result.structured:
            card = self._card(ev_id, capability, date.today().isoformat(), "no_data",
                              [], [f"{capability}: {result.status}"], "low")
            card["capability"] = capability
            derived = self._derive_capability(ev_id, capability, universe, candidates)
            if derived is not None:
                self._persist(derived)
                return derived
            self._persist(card)
            return card

        ok, reason = validate_card(result.structured)
        if not ok:
            card = self._card(ev_id, capability, date.today().isoformat(), "no_data",
                              [], [f"{capability}: {reason}"], "low")
            card["capability"] = capability
            derived = self._derive_capability(ev_id, capability, universe, candidates)
            if derived is not None:
                self._persist(derived)
                return derived
            self._persist(card)
            return card

        asof = str(result.structured.get("asof"))
        card_gaps2 = list(result.structured.get("gaps") or [])
        items = _norm_items(result.structured, dropped=card_gaps2)
        charts = _norm_charts(result.structured, dropped=card_gaps2)
        if not charts:
            auto = _fallback_chart({"ev_id": ev_id, "slot": capability,
                                    "asof": asof, "items": items})
            charts = [auto] if auto else []
        card = self._card(ev_id, capability, asof, "ok", items,
                          card_gaps2,
                          str(result.structured.get("confidence") or "medium"),
                          tool_calls=result.tool_calls, turns=result.turns,
                          charts=charts)
        card["capability"] = capability
        self._persist(card)
        return card

    def _derive_consensus(self, ev_id: str, universe: str) -> Optional[Dict[str, Any]]:
        """板块一致预期兜底：研报摘要 → 评级分布/目标价/近期观点。失败返回 None。"""
        from agents.deep_report.derive import summarize_consensus

        try:
            rows = call_with_timeout(lambda: self.consensus_fetcher(universe),
                                     _DERIVE_TIMEOUT_S) or []
        except TimeoutError:
            logger.info("一致预期聚合超时 universe=%s (>%.0fs)", universe, _DERIVE_TIMEOUT_S)
            return None
        except Exception as e:  # noqa: BLE001
            logger.info("一致预期聚合失败 universe=%s: %s", universe, e)
            return None
        if not rows:
            return None
        summary = summarize_consensus(rows, universe=universe,
                                     asof=date.today().isoformat())
        return {
            "ev_id": ev_id, "slot": "consensus", "capability": "consensus",
            "asof": summary["asof"], "status": summary["status"],
            "items": summary["items"], "charts": summary["charts"],
            "gaps": summary["gaps"], "confidence": summary["confidence"],
            "tool_calls": 0, "turns": 0,
        }

    def _derive_peer(self, ev_id: str, capability: str, universe: str,
                     tiers: Optional[Dict[str, List[Dict[str, str]]]]) -> Optional[Dict[str, Any]]:
        """逐股类能力：按模板的 stock_tiers 并行取数（财务横截面 / 多股归一化走势）。"""
        from datetime import timedelta

        from agents.deep_report.peer import summarize_peer_financials, summarize_peer_series

        flat: List[Dict[str, str]] = []
        for tier, items in (tiers or {}).items():
            for it in items or []:
                if isinstance(it, dict) and it.get("code"):
                    flat.append({"tier": str(tier), "name": str(it.get("name") or it["code"]),
                                 "code": str(it["code"])})
        if not flat:
            return None

        import tools.fetcher as _fetcher_mod

        def _fn(name: str):
            return getattr(_fetcher_mod, name)

        from concurrent.futures import ThreadPoolExecutor

        def _pmap(fn, items, workers):
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
                return list(pool.map(fn, items))

        asof = date.today().isoformat()
        if capability == "peer_financials":
            def _one(st):
                # 实测（2026-11-02）：并发 4 时相互拖死、6/10 失败；串行单只 2–4.5s、10/10 成功。
                # 财务接口不值得为了省 30 秒牺牲完整性。
                last_err = ""
                for _attempt in range(2):
                    try:
                        fin = call_with_timeout(lambda: _fn("ak_financial_abstract")(st["code"]), 45)
                        fin_rows = fin.to_dict("records") if hasattr(fin, "to_dict") else []
                        if not fin_rows:
                            last_err = "财报为空"
                            continue
                        try:
                            val = call_with_timeout(
                                lambda: _fn("ak_valuation_indicators")(st["code"]), 45)
                            val_rows = val.to_dict("records") if hasattr(val, "to_dict") else []
                        except Exception:  # noqa: BLE001 —— 估值缺失不影响财务横截面
                            val_rows = []
                        return {**st, "financial": fin_rows,
                                "valuation": (val_rows[-1] if val_rows else {})}
                    except Exception as e:  # noqa: BLE001
                        last_err = str(e)[:80]
                return {**st, "error": last_err or "empty"}

            rows = [_one(st) for st in flat]
            summary = summarize_peer_financials(rows, universe=universe, asof=asof)
        else:
            start = (date.today() - timedelta(days=150)).isoformat()

            def _hist(st):
                try:
                    df = call_with_timeout(
                        lambda: _fn("ak_stock_hist")(st["code"], "daily", start, asof), 30)
                    recs = df.to_dict("records") if hasattr(df, "to_dict") else []
                    out = []
                    for r in recs:
                        d = r.get("date") or r.get("trade_date") or r.get("日期")
                        c = r.get("close") or r.get("收盘")
                        if d and c:
                            out.append({"date": str(d)[:10], "close": float(c)})
                    return st["name"], out
                except Exception:  # noqa: BLE001
                    return st["name"], []

            results = _pmap(_hist, flat, 8)
            summary = summarize_peer_series({n: rows for n, rows in results if rows},
                                           universe=universe, asof=asof)

        card = {
            "ev_id": ev_id, "slot": capability, "capability": capability,
            "asof": summary["asof"], "status": summary["status"],
            "items": summary["items"], "charts": summary["charts"],
            "gaps": summary["gaps"], "confidence": summary["confidence"],
            "tool_calls": 0, "turns": 0,
        }
        if summary.get("inject_limit"):
            card["inject_limit"] = summary["inject_limit"]
        return card

    def _derive_capability(self, ev_id: str, capability: str, universe: str,
                           candidates: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """能力失败时的兜底推导（估值/一致预期/成分股）。没有兜底就返回 None。"""
        try:
            if capability == "valuation":
                return self._derive_valuation(ev_id, universe, candidates)
            if capability == "consensus":
                return self._derive_consensus(ev_id, universe)
            if capability == "constituents":
                return self._derive_constituents(ev_id, universe, candidates)
        except Exception:  # noqa: BLE001 —— 兜底失败只是 no_data
            logger.warning("兜底推导失败 capability=%s", capability, exc_info=True)
        return None

    def _board_name_of(self, universe: str,
                       candidates: Optional[List[Dict[str, Any]]] = None) -> str:
        for c in candidates or []:
            if c.get("kind") == "board":
                return str(c.get("name") or "")
        return re.sub(r"(板块|行业|概念|指数)$", "", universe or "")

    def _derive_constituents(self, ev_id: str, universe: str,
                             candidates: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """成分股兜底：按**板块名称**取成分（get_board_stocks 也要名称，指数代码取不到）。"""
        from agents.deep_report.derive import summarize_constituents

        board_name = self._board_name_of(universe, candidates)
        if not board_name:
            return None
        try:
            df = call_with_timeout(lambda: self.derive_fetcher(board_name), _DERIVE_TIMEOUT_S)
            rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df or [])
        except TimeoutError:
            logger.info("成分股自算超时 board=%s (>%.0fs)", board_name, _DERIVE_TIMEOUT_S)
            return None
        except Exception as e:  # noqa: BLE001
            logger.info("成分股自算失败 board=%s: %s", board_name, e)
            return None
        if not rows:
            return None
        summary = summarize_constituents(rows, board_name=board_name,
                                        asof=date.today().isoformat())
        return {
            "ev_id": ev_id, "slot": "constituents", "capability": "constituents",
            "asof": summary["asof"], "status": summary["status"],
            "items": summary["items"], "charts": summary["charts"],
            "gaps": summary["gaps"], "confidence": summary["confidence"],
            "tool_calls": 0, "turns": 0,
        }

    def _derive_valuation(self, ev_id: str, universe: str,
                          candidates: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """板块估值兜底：成分股截面聚合（中位数/加权/分布）。失败返回 None。"""
        from agents.deep_report.derive import summarize_board_valuation

        board_name = ""
        for c in candidates or []:
            if c.get("kind") == "board":
                board_name = str(c.get("name") or "")
                break
        if not board_name:
            board_name = re.sub(r"(板块|行业|概念|指数)$", "", universe or "")
        if not board_name:
            return None
        try:
            df = call_with_timeout(lambda: self.derive_fetcher(board_name), _DERIVE_TIMEOUT_S)
            rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df or [])
        except TimeoutError:
            logger.info("板块估值自算超时 board=%s (>%.0fs)", board_name, _DERIVE_TIMEOUT_S)
            return None
        except Exception as e:  # noqa: BLE001 —— 兜底失败只是 no_data，不影响报告
            logger.info("板块估值自算失败 board=%s: %s", board_name, e)
            return None
        if not rows:
            return None
        summary = summarize_board_valuation(rows, board_name=board_name,
                                           asof=date.today().isoformat())
        return {
            "ev_id": ev_id, "slot": "valuation", "capability": "valuation",
            "asof": summary["asof"], "status": summary["status"],
            "items": summary["items"], "charts": summary["charts"],
            "gaps": summary["gaps"], "confidence": summary["confidence"],
            "tool_calls": 0, "turns": 0,
        }

    def _card(self, ev_id: str, slot: str, asof: str, status: str,
              items: List[Dict[str, Any]], gaps: List[str], confidence: str,
              *, tool_calls: int = 0, turns: int = 0,
              charts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {"ev_id": ev_id, "slot": slot, "asof": asof, "status": status,
                "items": items, "charts": charts or [], "gaps": gaps,
                "confidence": confidence, "tool_calls": tool_calls, "turns": turns}

    def _persist(self, card: Dict[str, Any]) -> None:
        try:
            self.store.save_evidence(self.rid, card)
        except Exception:
            logger.warning("结论卡落盘失败 ev=%s", card.get("ev_id"), exc_info=True)


def summary_lines(card: Dict[str, Any], limit: int = 6) -> List[str]:
    """结论卡 → 注入写作的摘要行（只给数字与来源，不给原始 JSON）。

    多期 items 用**自己的 period**：序列里每个点日期不同，一律标卡片的 asof 会把
    「10-29 的点位」写成「10-30 的点位」（静默错日期）。
    """
    if card.get("status") != "ok":
        return [f"- {card.get('slot')}：数据缺失（{'；'.join(card.get('gaps') or []) or '未取到'}）"]
    items = card.get("items") or []
    anchors = [it for it in items if not str(it.get("period") or "").strip()]
    latest: Dict[str, Dict[str, Any]] = {}
    for it in items:
        period = str(it.get("period") or "").strip()
        if not period:
            continue
        metric = str(it.get("metric"))
        cur = latest.get(metric)
        # 与 QA 的 _best_entry 保持同一口径：按 period 取**最新**，不是「最后一条」
        if cur is None or period > str(cur.get("period") or ""):
            latest[metric] = it
    chosen = anchors + list(latest.values())
    lines = []
    for it in chosen[:limit]:
        unit = it.get("unit") or ""
        asof = it.get("period") or card.get("asof")
        lines.append(f"- {it['metric']}：{it['value']}{unit}（{it['source']}，{asof}）")
    return lines

