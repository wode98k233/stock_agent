"""全局总览聚合编排：块注册表 + 块级缓存 + 单飞 + SSE 事件。

设计：docs/superpowers/specs/2026-09-29-global-overview-design.md
硬约束：所有 loader 只调用现有服务层函数，不新增查询口径。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from config import Config

logger = logging.getLogger("radar.overview")

STALE_CACHE_MAX_S = 1800.0
_SLOW_BLOCK_TIMEOUT_S = 20.0
_SCAN_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class Block:
    id: str
    title: str
    zone: str
    kind: str
    span: int
    ttl: float
    loader: Callable[[dict], dict]
    timeout: float = 15.0


def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_of_trading_date() -> str:
    """最近已完成交易日 YYYY-MM-DD（信号时效基准确权用）；异常回退今天。"""
    try:
        from utils.trading_calendar import get_effective_trading_date
        return get_effective_trading_date("cn").isoformat()
    except Exception:
        logger.debug("[overview] 最近已完成交易日获取失败，回退今天", exc_info=True)
        return datetime.now().strftime("%Y-%m-%d")


def _ttl_for(block: Block) -> float:
    """块级缓存 TTL；配置键在任务 15 接入前用带默认值的 getattr 兜底。"""
    if block.id == "strategy_scan":
        return float(getattr(Config, "OVERVIEW_SCAN_TTL_S", 600))
    if block.id == "runtime":
        return 0.0
    if block.kind == "market":
        return 5.0
    return float(getattr(Config, "OVERVIEW_BLOCK_CACHE_TTL_S", 60))


# ── loaders（编排层：取数全部来自现有服务层） ────────────────────────────

def _load_market(ctx: dict) -> dict:
    from server import market_dashboard_service as svc
    from server import shortline_service
    data = svc.build_snapshot(ctx.get("skill_register"))
    try:
        hist = shortline_service.get_history(days=10)
        data["sentiment_history"] = [r.get("score") for r in hist if r.get("score") is not None]
    except Exception:
        data["sentiment_history"] = []
    return data


def _load_shortline(ctx: dict) -> dict:
    from server import shortline_service as svc
    return {"snapshot": svc.build_overview(use_cache=not ctx.get("force"))}


def _load_thesis(ctx: dict) -> dict:
    from server import thesis_service as svc
    items = []
    for t in svc.list_theses():
        tid = t["thesis_id"]
        history = svc.get_history(tid, days=20)
        try:
            detail = svc.get_detail(tid)
            snapshot = (detail or {}).get("snapshot") or {}
        except Exception:
            snapshot = {}
        items.append({**t,
                      "history": history.get("items", []),
                      "factors": snapshot.get("factors", []),
                      "signals": snapshot.get("signals", []),
                      "stages": snapshot.get("stages")})
    return {"items": items, "alerts": svc.get_alerts(days=7)}


def _load_portfolio(ctx: dict) -> dict:
    from server.services.portfolio_view import build_positions_view
    return build_positions_view(ctx["web_storage"].portfolio, "sina")


def _load_watchlist(ctx: dict) -> dict:
    from server.watchlist_service import fetch_quotes_for_symbols, persist_quotes
    ws = ctx["web_storage"].watchlist
    items = ws.list_stocks(None)
    quotes = {}
    codes = [it["stock_code"] for it in items]
    if codes:
        # 内部预算：外层 block 软超时（20s）会直接判块失败，导致整块无数据；
        # 这里自留更短的预算，超时即用缓存行情降级，让区块「成功但有降级」
        # 而不是「失败」。正常路径（hithink 批量快照）仅需 ~0.7s。
        budget = float(os.getenv("OVERVIEW_WATCHLIST_BUDGET_S", "8"))
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            quotes = pool.submit(
                fetch_quotes_for_symbols, codes, "auto"
            ).result(timeout=budget)
            if quotes:
                persist_quotes(ws, quotes)
        except TimeoutError:
            logger.warning("[overview] 自选行情超出 %.1fs 预算，降级用缓存行情", budget)
        except Exception as e:
            logger.warning("[overview] 自选行情失败（回退缓存行情）: %s", e)
        finally:
            # wait=False：超时后不能阻塞等待慢 worker 跑完，否则降级失去意义
            pool.shutdown(wait=False)
    merged = []
    for it in items:
        q = quotes.get(it["stock_code"], {})
        merged.append({**it,
                       "price": q.get("price", it.get("price")),
                       "change_pct": q.get("change_pct", it.get("change_pct"))})
    return {"items": merged, "quote_source": "auto"}


def _scan_codes(ctx: dict) -> list[str]:
    codes: set[str] = set()
    try:
        codes |= {p["stock_code"] for p in ctx["web_storage"].portfolio.list_positions()
                  if (p.get("qty") or 0) > 0}
    except Exception:
        pass
    try:
        codes |= {it["stock_code"] for it in ctx["web_storage"].watchlist.list_stocks(None)}
    except Exception:
        pass
    return sorted(c for c in codes if re.fullmatch(r"\d{6}", c))


# ── 策略买卖条件短语（徽标文案；模板定义见 backtest/strategies/json_templates/） ──

_FUNC_LABEL = {"macd": "MACD", "dpo": "DPO", "kdj": "KDJ", "rsi": "RSI",
               "boll": "BOLL", "ma": "均线", "sma": "均线", "ema": "均线",
               "dma": "DMA", "cyw": "CYW",
               "close": "收盘价", "high": "最高价", "low": "最低价", "volume": "成交量"}
_OP_LABEL = {"cross_above": "金叉", ">": "上破", ">=": "站上", "<": "跌破", "<=": "跌破"}
_FIELD_LABEL = {"upper": "上轨", "lower": "下轨", "mid": "中轨",
                "dif": "", "dea": "", "dpo": "", "madpo": "", "k": ""}
_PARAM_WORD = {"oversold": "超卖", "overbought": "超买"}


def _rule_func(rule: dict) -> str:
    left = rule.get("left") if isinstance(rule, dict) else None
    if not isinstance(left, dict):
        return ""
    return str(left.get("func") or left.get("data") or "").lower()


def _condition_rule_label(rule: dict, *, side: str) -> str | None:
    """单条条件的短语；未知形状或方向不符（买：死叉/超买；卖：金叉/超卖）返回 None。"""
    left = rule.get("left") if isinstance(rule, dict) else None
    right = rule.get("right") if isinstance(rule, dict) else None
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    op = str(rule.get("op") or "")
    if side == "buy" and op == "cross_below":  # 买入标签不产出「死叉」措辞
        return None
    if side == "sell" and op == "cross_above":  # 卖出标签不产出「金叉」措辞
        return None
    fname = _FUNC_LABEL.get(str(left.get("func") or left.get("data") or "").lower())
    if not fname:
        return None
    rfunc = str(right.get("func") or "").lower()
    rfield = str(right.get("field") or "").lower()
    rvalue = right.get("value")
    if rfield in ("upper", "lower"):
        rf = "布林" if rfunc == "boll" else (_FUNC_LABEL.get(rfunc) or "")
        if not rf:
            return None
        return ("突破" if rfield == "upper" else "跌破") + rf + _FIELD_LABEL[rfield]
    if isinstance(rvalue, str) and rvalue.startswith("{") and rvalue.endswith("}"):
        if op == "cross_above":  # 卖出已在入口拦截
            return f"{fname} 上穿零轴"
        word = _PARAM_WORD.get(rvalue[1:-1].lower())
        if not word or (side == "buy" and word == "超买") \
                or (side == "sell" and word == "超卖"):
            return None
        if side == "buy":
            return f"{fname} {word}回升" if op == ">" else f"{fname} {word}"
        return f"{fname} {word}回落" if op == "<" else f"{fname} {word}"
    if op == "cross_above":
        return f"{fname} 上穿零轴" if rvalue == 0 else f"{fname} 金叉"
    if op == "cross_below":  # 买入已在入口拦截，仅卖出走到这里
        return f"{fname} 下穿零轴" if rvalue == 0 else f"{fname} 死叉"
    # 指标线之间的 </>（如 macd<signal）是状态判断，不是价格击穿阈值 → 金叉/死叉措辞
    if op in (">", "<") and (left.get("func") or left.get("field")) \
            and (right.get("func") or right.get("field")):
        if op == ">":
            return None if side == "sell" else f"{fname} 金叉"  # 卖出标签不产出「金叉」
        return None if side == "buy" else f"{fname} 死叉"        # 买入标签不产出「死叉」
    if op in (">", ">=", "<", "<="):
        if rvalue == 0:
            return f"{fname} 站上零轴" if op in (">", ">=") else f"{fname} 水下"
        if isinstance(rvalue, (int, float)) and not isinstance(rvalue, bool):
            return f"{fname} 低位" if op in ("<", "<=") else f"{fname} 上破"
        if rfunc:
            return f"{fname} {_OP_LABEL[op]}"
    return None


def _derive_condition_label(rules: list[dict] | None, *, side: str) -> str | None:
    """由 conditions.{side}.rules 合成条件短语（如 'MACD 金叉'/'KDJ 超买'）。无法识别返回 None。"""
    if not isinstance(rules, list) or not rules:
        return None
    labels: list[str] = []
    funcs: set[str] = set()
    ops: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            return None
        label = _condition_rule_label(rule, side=side)
        if not label:
            return None
        labels.append(label)
        funcs.add(_rule_func(rule))
        ops.add(str(rule.get("op") or ""))
    if len(labels) == 1:
        return labels[0]
    # 单指标互为补充（如 DPO 站上超卖线且仍在零轴下）→ 折叠为该指标主短语
    collapse_word = "超卖回升" if side == "buy" else "超买回落"
    if len(funcs) == 1 and ops == {">", "<"} and any(collapse_word in x for x in labels):
        fname = _FUNC_LABEL.get(next(iter(funcs)), "")
        if not fname:
            return None
        return f"{fname} 超卖区回升" if side == "buy" else f"{fname} 超买区回落"
    if len(set(labels)) == 1:
        return labels[0]
    if len(funcs) == 1 and len(ops) == 1:
        return labels[0]
    return "多指标共振"


def _load_strategy_scan(ctx: dict) -> dict:
    from backtest.signal_scanner import SignalScanner
    codes = _scan_codes(ctx)
    as_of = _as_of_trading_date()
    if not codes:
            return {"hits": {}, "sells": {}, "scanned": 0, "names": {}, "labels": {},
                    "sell_labels": {}, "trends": {}, "as_of": as_of,
                    "summary": {"codes": 0, "hit_codes": 0, "signals": 0,
                                "sell_codes": 0, "sell_signals": 0, "strategies": 0}}
    configured = [s.strip() for s in (getattr(Config, "OVERVIEW_SCAN_STRATEGIES", "") or "").split(",") if s.strip()]
    emit_progress = ctx.get("emit_progress")

    def _progress(done, total, label):
        if emit_progress:
            emit_progress({"event": "block_progress", "data": {
                "id": "strategy_scan", "done": done, "total": total,
                "label": label, "ts": _iso()}})

    raw = SignalScanner().scan_many(
        codes, configured or None,
        days=getattr(Config, "OVERVIEW_SCAN_DAYS", 120),
        max_workers=4, progress=_progress,
        refresh_stale_max=int(getattr(Config, "OVERVIEW_SCAN_REFRESH_MAX", 6)),
        # 补尾预算必须**远小于**块软超时（_SCAN_TIMEOUT_S=90s）：补尾是网络密集的
        # 串行操作，不限时会让它吃光整块预算 → 块先判超时、worker 仍在跑、
        # 进度迟到、结果被丢弃（2026-09-30 实测「卡 3 分钟」的完整机制）。
        refresh_stale_budget_s=float(
            getattr(Config, "OVERVIEW_SCAN_REFRESH_BUDGET_S", 20)))
    hits = {c: v for c, v in raw.items() if v.get("hits")}
    # sells 与 hits 对称：每 code 容器带 latest_date，供前端按新旧分三档配色
    sells = {c: {"sells": v["sells"], "latest_date": v.get("latest_date") or ""}
             for c, v in raw.items() if v.get("sells")}
    names: dict[str, str] = {}
    labels: dict[str, str] = {}
    sell_labels: dict[str, str] = {}
    try:
        from utils.cache.backtest_db import get_strategies
        for r in get_strategies():
            sid = r.get("strategy_id")
            if not sid:
                continue
            if r.get("name"):
                names[sid] = r["name"]
            try:
                code_obj = json.loads(r.get("code") or "")
                conditions = code_obj.get("conditions") or {}
                buy_rules = (conditions.get("buy") or {}).get("rules")
                sell_rules = (conditions.get("sell") or {}).get("rules")
            except Exception:
                logger.debug("[overview] 策略 %s 定义解析失败（跳过标签）", sid, exc_info=True)
                continue
            label = _derive_condition_label(buy_rules, side="buy")
            if label:
                labels[sid] = label
            sell_label = _derive_condition_label(sell_rules, side="sell")
            if sell_label:
                sell_labels[sid] = sell_label
    except Exception:
        names, labels, sell_labels = {}, {}, {}
    try:
        from backtest.strategies import get_all_strategies
        strategy_count = len(configured) if configured else len(get_all_strategies())
    except Exception:
        strategy_count = len(configured)
    trends = {c: v.get("closes") or [] for c, v in raw.items() if v.get("closes")}
    return {"hits": hits, "sells": sells, "scanned": len(codes), "names": names,
            "labels": labels, "sell_labels": sell_labels, "trends": trends,
            "as_of": as_of,
            "summary": {"codes": len(codes), "hit_codes": len(hits),
                        "signals": sum(len(v["hits"]) for v in hits.values()),
                        "sell_codes": len(sells),
                        "sell_signals": sum(len(v["sells"]) for v in sells.values()),
                        "strategies": strategy_count}}


def _load_macro(ctx: dict) -> dict:
    from server.macro_service import build_macro_board
    configured = [s.strip() for s in (getattr(Config, "OVERVIEW_MACRO_INDICATORS", "") or "").split(",") if s.strip()]
    return build_macro_board(configured or None)


def _signal_overview() -> dict | None:
    """signal-eval 费后收益统计（服务自身带 300s 缓存）；失败 → None，不拖垮质量块。"""
    try:
        from server import signal_eval_service as ses
        return ses.build_overview(0.0)
    except Exception as e:
        logger.warning("[overview] signal overview 加载失败: %s", e)
        return None


def _load_quality(ctx: dict) -> dict:
    from server import quality_service as qs
    dbs = qs.open_quality_dbs()
    try:
        return qs.build_overview(dbs, signal_overview=_signal_overview())
    finally:
        qs.close_quality_dbs(dbs)


def _load_review(ctx: dict) -> dict:
    from eval.review.store import aggregate_reviews
    from eval.storage.eval_db import get_eval_storage
    from server import signal_eval_service as ses
    from server.review_service import get_trace_conn

    st = get_eval_storage()
    runs = st.list_runs(limit=1)
    latest = st.get_run_summary(runs[0]["id"]) if runs else None
    conn = get_trace_conn()
    return {
        "eval": {"latest": latest, "trend": st.get_trend(limit=7)},
        "reviews": {"run": aggregate_reviews(conn, "run"),
                    "dialog": aggregate_reviews(conn, "dialog")},
        "signal": ses.build_overview(0.0),
        "calibration": ses.build_calibration(0.0),
    }


def _token_cost(agg: dict, price_in: float, price_out: float, price_cached: float) -> dict:
    """按单价（元/百万 token）估算成本。

    输入 token 中缓存命中部分按 cache 单价计，未命中部分按 input 单价计；
    输出 token 全部按 output 单价计。缓存 token 大于输入时按输入封顶（防脏数据放大）。
    """
    inp = agg.get("input_tokens", 0) or 0
    out = agg.get("output_tokens", 0) or 0
    cached = min(agg.get("cached_tokens", 0) or 0, inp)
    fresh_in = max(0, inp - cached)
    unit = 1_000_000.0
    cost_in = fresh_in / unit * price_in
    cost_cached = cached / unit * price_cached
    cost_out = out / unit * price_out
    return {
        "total": round(cost_in + cost_cached + cost_out, 4),
        "input": round(cost_in, 4),
        "cached": round(cost_cached, 4),
        "output": round(cost_out, 4),
        "unit": "元",
    }


def _load_usage(ctx: dict) -> dict:
    from utils.agent_trace.store import store as trace_store

    ws = ctx["web_storage"]
    today = datetime.now().date()
    since = (datetime.now() - timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S")
    dialogs = ws.count_dialogs_since(since)
    messages = ws.count_messages_since(since)

    runs = trace_store.list_runs(limit=2000, since=since)
    day_index = {(today - timedelta(days=13 - i)).isoformat(): i for i in range(14)}
    activity = [0] * 14
    mode_dist: dict[str, int] = {}
    duration_sum = duration_n = success = 0
    for r in runs:
        day = str(r.get("created_at") or "")[:10]
        if day in day_index:
            activity[day_index[day]] += 1
        mode = r.get("agent_name") or "unknown"
        mode_dist[mode] = mode_dist.get(mode, 0) + 1
        ms = r.get("duration_ms") or 0
        if ms:
            duration_sum += ms
            duration_n += 1
        if r.get("status") == "success":
            success += 1

    # ── token 用量与成本（跨月库存量，缓存命中步骤不计） ──
    token_days = int(getattr(Config, "OVERVIEW_TOKEN_DAYS", 14) or 14)
    token = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "cached_tokens": 0, "reasoning_tokens": 0, "cache_hit_ratio": 0.0,
             "avg_tokens_per_call": 0, "daily": [0] * token_days,
             "by_agent": [], "by_model": []}
    try:
        token = trace_store.aggregate_llm_usage(since=since, days=token_days)
    except Exception as e:
        logger.warning("[overview] token 聚合失败: %s", e)
    price_in = float(getattr(Config, "OVERVIEW_TOKEN_PRICE_IN", 2.0) or 0)
    price_out = float(getattr(Config, "OVERVIEW_TOKEN_PRICE_OUT", 8.0) or 0)
    price_cached = float(getattr(Config, "OVERVIEW_TOKEN_PRICE_CACHED", 0.2) or 0)
    cost = _token_cost(token, price_in, price_out, price_cached)

    return {
        "dialogs": dialogs, "messages": messages, "runs": len(runs),
        "success_rate": round(success / len(runs) * 100, 1) if runs else None,
        "avg_sec": round(duration_sum / duration_n / 1000, 1) if duration_n else None,
        "activity": activity,
        "modes": sorted(mode_dist.items(), key=lambda kv: -kv[1])[:4],
        "recent": ws.list_dialogs(limit=5),
        "token": token,
        "cost": cost,
        "token_days": token_days,
        "pricing": {"model": getattr(Config, "OVERVIEW_TOKEN_MODEL", "DeepSeek v4.1 Flash"),
                    "in": price_in, "out": price_out, "cached": price_cached},
    }


def _load_thesis_portfolio(ctx: dict) -> dict:
    """组合层：跨资产配比 + 共同风险暴露（研究类）。

    为什么单独成块：组合层**不是** thesis 定义（跨资产组合没有单一 index_code，
    放 data/thesis/ 会被 validate_def 判成缺 subject 的假台账），所以它不会像
    新台账那样被 `_load_thesis` 自动带出来，必须显式挂一块。

    只读各台账既有快照 + 本地日线自算相关性，不触发取数，故用默认块超时即可。
    """
    from server import thesis_service as svc
    try:
        return {"items": svc.list_portfolios(), "ok": True}
    except Exception as e:
        logger.warning("[overview] 组合层读取失败: %s", e)
        return {"items": [], "ok": False, "reason": str(e)[:120]}


def _load_todos(ctx: dict) -> dict:
    from server import thesis_service as ts
    from server.monitoring.store import MonitorStore
    from utils import data_monitor
    from utils.cache.backtest_db import get_data_tasks

    items: list[dict] = []
    try:
        unread = MonitorStore().unread_count()
        if unread:
            items.append({"kind": "monitor", "severity": "warn",
                          "text": f"盯盘未读告警 {unread} 条", "target": "monitor", "time": ""})
    except Exception:
        pass
    try:
        for a in ts.get_alerts(days=7)[:5]:
            items.append({"kind": "thesis", "severity": a.get("severity") or "info",
                          "text": a.get("message", "")[:80],
                          "target": "thesis", "time": a.get("trade_date", "")})
    except Exception:
        pass
    try:
        for t in get_data_tasks(status="failed", limit=10)[:3]:
            label = t.get("task_type") or t.get("type") or "未知任务"
            items.append({"kind": "task", "severity": "error",
                          "text": f"采集任务失败：{label}", "target": "data", "time": ""})
    except Exception:
        pass
    try:
        rep = data_monitor.overview()
        bad = [a for a in rep.get("assets", []) if a.get("error")]
        if bad:
            items.append({"kind": "stale", "severity": "warn",
                          "text": f"数据资产异常 {len(bad)} 项", "target": "data",
                          "time": rep.get("scanned_at", "")})
    except Exception:
        pass
    return {"items": items}


def _load_storage(ctx: dict) -> dict:
    from utils import data_monitor
    from utils.cache.backtest_db import get_data_tasks

    rep = data_monitor.overview()
    assets = rep.get("assets", [])
    total_bytes = sum(a.get("size_bytes") or 0 for a in assets)
    errors = [a for a in assets if a.get("error")]
    cleanup = {"rules": 0, "items": 0, "size_bytes": 0}
    try:
        preview = data_monitor.cleanup_preview()
        cleanup = {"rules": len(preview),
                   "items": sum(p.get("count") or 0 for p in preview),
                   "size_bytes": sum(p.get("size") or 0 for p in preview)}
    except Exception as e:
        logger.warning("[overview] 清理预览失败: %s", e)
    failed_tasks = 0
    try:
        failed_tasks = len(get_data_tasks(status="failed", limit=50))
    except Exception:
        pass
    queue = 0
    try:
        queue = len(get_data_tasks(status="pending", limit=50))
    except Exception:
        pass
    cats: dict[str, int] = {}
    for a in assets:
        key = a.get("category") or "其他"
        cats[key] = cats.get(key, 0) + (a.get("size_bytes") or 0)
    categories = sorted(({"name": k, "mb": round(v / 1024 / 1024, 1)} for k, v in cats.items()),
                        key=lambda x: -x["mb"])
    return {"total_mb": round(total_bytes / 1024 / 1024, 1), "assets": len(assets),
            "stale": len(errors), "scan_at": rep.get("scanned_at"),
            "categories": categories[:6], "cleanup": cleanup,
            "failed_tasks": failed_tasks, "queue": queue}


def _load_runtime(ctx: dict) -> dict:
    from server.sysinfo import build_runtime_stats
    return build_runtime_stats()


# 顺序 = SSE 推送顺序：本库块在前（快），网络块在后
BLOCKS: list[Block] = [
    Block("macro", "宏观指标看板", "right", "research", 12, 0, _load_macro),
    Block("thesis", "研究台账", "center", "research", 12, 0, _load_thesis),
    Block("todos", "待办与提醒", "left", "research", 12, 0, _load_todos),
    Block("usage", "Agent 使用统计", "right", "research", 12, 0, _load_usage),
    Block("thesis_portfolio", "组合层配比", "center", "research", 12, 0, _load_thesis_portfolio),
    Block("quality", "质量观测", "center", "research", 6, 0, _load_quality),
    Block("review", "评测与信号胜率", "center", "research", 6, 0, _load_review),
    Block("storage", "数据中心存储", "left", "research", 12, 0, _load_storage),
    Block("runtime", "运行状态", "left", "system", 12, 0, _load_runtime),
    Block("market", "市场温度", "center", "market", 5, 0, _load_market, _SLOW_BLOCK_TIMEOUT_S),
    Block("shortline", "短线情绪", "center", "market", 7, 0, _load_shortline, _SLOW_BLOCK_TIMEOUT_S),
    Block("portfolio", "持仓概览", "center", "market", 7, 0, _load_portfolio, _SLOW_BLOCK_TIMEOUT_S),
    Block("watchlist", "自选速览", "center", "market", 5, 0, _load_watchlist, _SLOW_BLOCK_TIMEOUT_S),
    Block("strategy_scan", "策略扫描", "center", "system", 0, 0, _load_strategy_scan, _SCAN_TIMEOUT_S),
]

_run_cache: dict[str, tuple[float, dict]] = {}
_run_locks: dict[str, threading.Lock] = {}
_cache_guard = threading.Lock()


def _lock_for(bid: str) -> threading.Lock:
    with _cache_guard:
        return _run_locks.setdefault(bid, threading.Lock())


def _select_blocks(blocks: list[str] | None) -> list[Block]:
    if not blocks:
        return list(BLOCKS)
    wanted = set(blocks)
    return [b for b in BLOCKS if b.id in wanted]


def _load_block(block: Block, ctx: dict, force: bool) -> dict:
    """执行单块：缓存 / 单飞 / 软超时 / 失败回退旧缓存。"""
    started = time.time()
    ttl = _ttl_for(block)
    with _cache_guard:
        cached = _run_cache.get(block.id)

    def _hit():
        return {"ok": True, "cache": True, "stale_cache": False, "degraded": [],
                "ms": 0, "data": cached[1]}

    if not force and cached and ttl > 0 and (started - cached[0]) <= ttl:
        return _hit()
    with _lock_for(block.id):
        with _cache_guard:
            cached = _run_cache.get(block.id)
        if cached and cached[0] > started:
            # 单飞：本次调用开始后已有结果落缓存（同批并发请求），即使 force 也直接复用
            return _hit()
        if not force and cached and ttl > 0 and (time.time() - cached[0]) <= ttl:
            return _hit()
        try:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                payload = pool.submit(block.loader, ctx).result(timeout=block.timeout)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            with _cache_guard:
                _run_cache[block.id] = (time.time(), payload)
            return {"ok": True, "cache": False, "stale_cache": False,
                    "degraded": [], "ms": int((time.time() - started) * 1000), "data": payload}
        except Exception as e:
            reason = (f"超时 >{block.timeout:.0f}s"
                      if isinstance(e, TimeoutError) else f"{type(e).__name__}: {e}")
            if cached and (time.time() - cached[0]) <= STALE_CACHE_MAX_S:
                mins = int((time.time() - cached[0]) / 60)
                return {"ok": True, "cache": False, "stale_cache": True,
                        "degraded": [f"使用 {mins} 分钟前缓存：{reason}"],
                        "ms": int((time.time() - started) * 1000), "data": cached[1]}
            return {"ok": False, "cache": False, "stale_cache": False, "degraded": [],
                    "ms": int((time.time() - started) * 1000),
                    "error": reason, "data": None}


def run_stream(ctx: dict, blocks: list[str] | None = None, force: bool = False,
               emit=None) -> None:
    """并发执行所选块，完成即 emit（emit=None 时静默执行，供测试/快照复用）。"""
    selected = _select_blocks(blocks)
    ctx = {**ctx, "force": force}
    if emit:
        emit({"event": "plan", "data": {
            "blocks": [{"id": b.id, "title": b.title, "zone": b.zone, "kind": b.kind}
                       for b in selected],
            "force": force, "ts": _iso(),
            "market_refresh_s": int(getattr(Config, "OVERVIEW_MARKET_REFRESH_S", 60)),
            "runtime_refresh_s": int(getattr(Config, "OVERVIEW_RUNTIME_REFRESH_S", 10))}})
    started = time.time()
    ok = failed = 0
    degraded: list[str] = []

    def _run(block: Block, emit_progress=None):
        if emit:
            emit({"event": "block_start", "data": {"id": block.id, "ts": _iso()}})
        result = _load_block(block, {**ctx, "emit_progress": emit_progress}, force)
        if emit:
            if result["ok"]:
                emit({"event": "block_done", "data": {
                    "id": block.id, "ms": result["ms"], "cache": result["cache"],
                    "stale_cache": result["stale_cache"], "degraded": result["degraded"],
                    "data": result["data"], "ts": _iso()}})
            else:
                emit({"event": "block_error", "data": {
                    "id": block.id, "ms": result["ms"], "error": result["error"],
                    "ts": _iso()}})
        return block.id, result

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for b in selected:
            if b.id == "strategy_scan":
                ep = (lambda payload: emit(payload)) if emit else None
                futures.append(pool.submit(_run, b, ep))
            else:
                futures.append(pool.submit(_run, b))
        for fut in as_completed(futures):
            bid, result = fut.result()
            if result["ok"]:
                ok += 1
                if result["stale_cache"] or result["degraded"]:
                    degraded.append(bid)
            else:
                failed += 1
    if emit:
        emit({"event": "final", "data": {
            "total": len(selected), "ok": ok, "failed": failed,
            "elapsed_ms": int((time.time() - started) * 1000),
            "degraded": degraded, "ts": _iso()}})


def load_snapshot(ctx: dict, blocks: list[str] | None = None, force: bool = False) -> dict:
    """同步收集（JSON 快照端点 / 运行状态轮询用）。"""
    selected = _select_blocks(blocks)
    out: dict[str, dict] = {}
    for b in selected:
        result = _load_block(b, {**ctx, "force": force}, force)
        entry = {k: result[k] for k in ("ok", "cache", "stale_cache", "degraded", "ms")}
        entry["data" if result["ok"] else "error"] = (
            result["data"] if result["ok"] else result["error"])
        out[b.id] = entry
    return {"blocks": out, "generated_at": _iso()}
