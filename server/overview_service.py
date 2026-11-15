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
from datetime import datetime
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


def _strategy_health_map() -> dict[str, dict]:
    """策略 → 最近一次回测的可信度摘要（设计文档 §11）。

    硬约束：只读本地 backtest.db。**绝不**触发补数或网络请求——
    _load_strategy_scan 里那段补尾预算注释（本文件 refresh_stale_budget_s 处）记着教训：
    大屏块里做网络密集的串行补数会把整块拖死（块先判超时、worker 仍在跑、结果被丢弃）。

    「已验证」口径与大屏黄标、策略详情页同源（server/backtest_service.py:18-19
    的 VERIFIED_HEALTH / MIN_VERIFIED_TRADES），改一处必须改另一处。
    """
    from utils.cache.backtest_db import (get_strategies, get_backtest_runs,
                                         get_result_meta_map, get_backtest_eval)
    from server.backtest_service import VERIFIED_HEALTH, MIN_VERIFIED_TRADES
    out: dict[str, dict] = {}
    try:
        strategies = get_strategies()
    except Exception:
        logger.warning("[overview] 策略列表读取失败", exc_info=True)
        return out
    for s in strategies or []:
        sid = (s or {}).get("strategy_id") or ""
        if not sid:
            continue
        info: dict = {"strategy_id": sid, "name": (s or {}).get("name") or sid,
                      "verified": False, "status": "never", "last_run_at": "",
                      "coverage_pct": None, "trade_count": 0, "sharpe_ratio_io": None}
        try:
            rows, _total = get_backtest_runs(strategy_id=sid, limit=1, offset=0)
        except Exception:
            logger.warning("[overview] 策略回测记录读取失败 strategy_id=%s", sid, exc_info=True)
            out[sid] = info
            continue
        if rows:
            r = rows[0]
            run_id = r.get("run_id")
            meta = (get_result_meta_map([run_id]) or {}).get(run_id) or {}
            try:
                ev = get_backtest_eval(run_id) or {}
            except Exception:
                ev = {}
            health = meta.get("health") or ""
            trades = r.get("trade_count") or 0
            ok = (health in VERIFIED_HEALTH and meta.get("trade_sample_ok") in (1, True)
                  and trades >= MIN_VERIFIED_TRADES)
            info.update({
                "verified": bool(ok),
                # status 是「最近一次回测的体检结论」；verified 还要样本达标（两者不等价）
                "status": ("never" if not health else
                           "ok" if health in VERIFIED_HEALTH else
                           "gap" if health == "data_gap" else "weak"),
                "last_run_at": r.get("created_at") or "",
                "coverage_pct": meta.get("coverage_pct"),
                "trade_count": trades,
                "sharpe_ratio_io": ev.get("sharpe_ratio_io"),
            })
        out[sid] = info
    return out


def _load_strategy_health(ctx: dict) -> dict:
    """策略健康度块（设计文档 §11-1）：每个策略最近一次回测可不可信。

    刻意不接 ctx：不依赖扫描上下文，纯本地库读取（块加载器允许忽略 ctx）。
    """
    health = _strategy_health_map()
    rows = list(health.values())
    # 已验证的排前面；同组按最近回测时间倒序
    rows.sort(key=lambda r: (bool(r.get("verified")), r.get("last_run_at") or ""), reverse=True)
    summary = {
        "total": len(rows),
        "verified": sum(1 for r in rows if r.get("verified")),
        "never": sum(1 for r in rows if r.get("status") == "never"),
        "gap": sum(1 for r in rows if r.get("status") == "gap"),
        "weak": sum(1 for r in rows if r.get("status") == "weak"),
    }
    return {"rows": rows[:20], "summary": summary,
            "as_of": datetime.now().strftime("%Y-%m-%d %H:%M")}


def _load_strategy_scan(ctx: dict) -> dict:
    from backtest.signal_scanner import SignalScanner
    codes = _scan_codes(ctx)
    as_of = _as_of_trading_date()
    if not codes:
            return {"hits": {}, "sells": {}, "scanned": 0, "names": {}, "labels": {},
                    "sell_labels": {}, "trends": {}, "as_of": as_of,
                    "strategy_health": {},
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
    # 策略可信度（设计文档 §11-2）：供前端给「未经验证」的策略命中打黄标。
    # 取数失败只降级为「无可信度信息」（前端一律不打标），不拖垮扫描块本身。
    try:
        health_map = _strategy_health_map()
    except Exception:
        logger.warning("[overview] 策略可信度映射读取失败（扫描块降级）", exc_info=True)
        health_map = {}
    return {"hits": hits, "sells": sells, "scanned": len(codes), "names": names,
            "labels": labels, "sell_labels": sell_labels, "trends": trends,
            "as_of": as_of,
            "strategy_health": {sid: {"verified": bool(v.get("verified")),
                                      "status": v.get("status") or ""}
                                for sid, v in health_map.items()},
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


# ── 上下文洞察 / 活跃度 / 会话上下文档案（总览大屏研究组三块） ──────────────
# 键名事实源（2026-10-17 逐条打印核对过真实返回，勿凭记忆改）：
#   store.llm_steps_totals(run_ids) -> {run_id: [{"step_name","extra"}, ...]}
#       其中 extra 是 **JSON 字符串**，token 在 extra.token_usage、成本在 extra.cost
#   store.cost_totals_for_run(run_id) -> {"total","calls","unpriced_calls","free_calls",...}
#   calendar_query.query_trace_calendar(store, y, m)
#       -> {"year","month","run_days": {日期: [run 字典, ...]}}（外层键是 run_days）
#   ctx 里 storage 的键名是 "web_storage"（与既有 _load_portfolio 同一取法）


def _store_totals(run_ids: list) -> dict:
    """按 run_id 汇总 token / 成本 ->
    {run_id: {"total_tokens","reasoning_tokens","output_tokens","cost"}}。

    ⚠️ llm_steps_totals 的真实返回是**逐 step 列表**（不是聚合好的 dict），必须逐条解析
    extra 里的 token_usage / cost。成本与 build_context_insight 同口径：一次都没记到价
    （cost.total 全为 None）时给 None（未计价 / 从未记录），而不是 0.0 —— 免费 ≠ 0 元。
    """
    from utils.agent_trace.context_stats import parse_extra
    from utils.agent_trace.store import store
    from utils.token_usage import normalize_token_usage_dict

    if not run_ids:
        return {}
    try:
        raw = store.llm_steps_totals(run_ids) or {}
    except Exception as e:
        logger.warning("[overview] llm_steps_totals 失败: %s", e)
        return {}
    out: dict = {}
    for rid, steps in raw.items():
        total_tokens = reasoning_tokens = output_tokens = 0
        cost_sum = 0.0
        priced = False
        for step in steps or []:
            # ⚠️ extra 是自由 JSON：坏 JSON / JSON 列表 / 字符串 / null 都可能出现。
            # 解析一律走 context_stats.parse_extra（同族模块的唯一 extra 解析入口，
            # 非 dict 一律回空 dict）—— 脏值绝不能穿透到块级。
            extra = parse_extra(step.get("extra") if isinstance(step, dict) else None)
            # cache_hit 伪步骤（A8 缓存命中，非真实调用）跳过 —— 与
            # store.aggregate_llm_usage 的 extra.cache_hit 过滤同口径，
            # 否则本块与月库聚合 / context_stats 三个入口结论分叉。
            if not extra or extra.get("cache_hit"):
                continue
            # ⚠️ token 取值必须走仓库唯一归一入口：缺 total_tokens 时它回退
            # input + output。直读 extra.token_usage.total_tokens 会在缺该键时
            # 静默得 0 并累计进大屏 KPI（任务 2 在 _usage 上踩过同一写法）。
            # token_usage 非 dict（脏值）视为未采集，不抛。
            usage = extra.get("token_usage")
            if isinstance(usage, dict):
                usage = normalize_token_usage_dict(usage)
                total_tokens += int(usage.get("total_tokens") or 0)
                reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
                # 输出侧同样逐 step 累加：output_tokens 是「思考 + 回答」的分母（铁律 2），
                # 会话卡的迷你环 / 「占输出 X%」都靠它 —— 与上面两个量同源、同归一入口。
                output_tokens += int(usage.get("output_tokens") or 0)
            cost = extra.get("cost")
            amount = None
            if isinstance(cost, dict) and cost.get("total") is not None:
                try:
                    amount = float(cost["total"])
                except (TypeError, ValueError):
                    amount = None      # 脏值：按未计价处理，不抛、也不按 0 计入金额
            if amount is not None:
                cost_sum += amount
                priced = True
        out[rid] = {"total_tokens": total_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "output_tokens": output_tokens,
                    "cost": round(cost_sum, 6) if priced else None}
    return out


def _load_context_insight(ctx: dict) -> dict:
    """上下文洞察：KPI + 近 N 天双柱 + Token 构成（输出拆思考 / 回答）。

    ⚠️ 本 loader 是**委托** context_stats.build_context_insight —— 空区间与三态护栏
    已经做在那里（命中率判**分母** input_tokens；cost 三态：已计价 / 免费 0.0 /
    未计价或从未记录 -> None）。这里**不许**再写一遍守卫：改用更差的判据（llm_calls）
    会把任务 7 审查抓出的 bug（「有调用但全缺 token_usage」误报命中 0%）引回来。
    """
    from utils.agent_trace import context_stats

    days = int(getattr(Config, "OVERVIEW_TOKEN_DAYS", 14) or 14)
    return context_stats.build_context_insight(days=days)


def _load_activity(ctx: dict) -> dict:
    """近 8 周活跃度热力图，复用 calendar_query 的按月聚合（不重写聚合）。

    ⚠️ 返回结构外层键是 **run_days**（不是 dialog_days），run 字典里取 total_tokens。
    """
    from datetime import date

    from utils.agent_trace.calendar_query import query_trace_calendar
    from utils.agent_trace.store import store

    today = date.today()
    by_day: dict = {}
    for back in range(7, -1, -1):
        index = today.year * 12 + (today.month - 1) - back
        year, month = index // 12, index % 12 + 1
        try:
            payload = query_trace_calendar(store, year, month) or {}
        except Exception as e:
            logger.warning("[overview] 活跃度取 %s-%s 失败: %s", year, month, e)
            continue
        for day, runs in (payload.get("run_days") or {}).items():
            by_day[day] = sum(int((r or {}).get("total_tokens") or 0) for r in runs)
    # 日期升序（热力图按周排列；月库内返回顺序不保证）
    days_list = [{"date": d, "tokens": by_day[d]} for d in sorted(by_day)]
    return {"days": days_list[-56:]}


def _load_context_sessions(ctx: dict, days: int = 7) -> dict:
    """会话上下文档案。标题来自 web_dialogs，run 映射来自 latest_trace_runs_for_dialogs。"""
    from datetime import datetime, timedelta, timezone

    storage = ctx.get("web_storage")
    if storage is None:
        return {"sessions": [], "days": days}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        dialogs = [d for d in (storage.list_dialogs(limit=200) or [])
                   if (d.get("updated_at") or d.get("created_at") or "") >= since]
    except Exception as e:
        logger.warning("[overview] 会话档案取对话失败: %s", e)
        return {"sessions": [], "days": days}
    try:
        run_map = storage.latest_trace_runs_for_dialogs(
            [d.get("dialog_uuid") for d in dialogs]) or {}
    except Exception as e:
        logger.warning("[overview] 会话档案取 run 映射失败: %s", e)
        run_map = {}
    try:
        totals = _store_totals([rid for rid in run_map.values() if rid])
    except Exception as e:
        # 与上面 run_map 同款降级：单个脏 step 不能让整块档案失败 —— 照常列出会话，
        # token / 成本留 None（未采集），不显示成 0。
        logger.warning("[overview] 会话档案汇总 token 失败: %s", e)
        totals = {}
    sessions = []
    for dialog in dialogs:
        uuid = dialog.get("dialog_uuid")
        run_id = run_map.get(uuid)
        agg = totals.get(run_id) or {}
        sessions.append({"dialog_uuid": uuid, "title": dialog.get("title") or "未命名",
                         "agent_mode": dialog.get("current_mode"), "run_id": run_id,
                         "created_at": dialog.get("created_at"),
                         "billed_tokens": agg.get("total_tokens"),
                         "cost": agg.get("cost"),
                         "reasoning_tokens": agg.get("reasoning_tokens"),
                         # 输出总量：卡片据此算 thinking = min(reasoning, output)、
                         # answer = output - thinking（思考 + 回答 ≡ 输出）。
                         # 缺 run / 汇总失败 -> None = 未采集（前端只画轨道，不画假 0）。
                         "output_tokens": agg.get("output_tokens")})
    return {"sessions": sessions, "days": days}


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
    # 总览大屏研究组三块（任务 11）：usage 块 id 保持不变（前端与既有测试按 id 取），
    # 只换标题与 loader —— payload 形状随之变更。
    # ⚠️ 旧 usage 形状（dialogs/messages/token/cost/pricing 那套）已随任务 13 清理**整链删除**，
    # 别再按旧键去对前端适配器（以实现为准：本块现在只回 _load_context_insight 的键）。
    Block("usage", "上下文洞察", "right", "research", 12, 0, _load_context_insight),
    Block("activity", "活跃度", "left", "research", 12, 0, _load_activity),
    Block("sessions", "会话上下文档案", "center", "research", 0, 0,
          _load_context_sessions, _SLOW_BLOCK_TIMEOUT_S),
    Block("thesis_portfolio", "组合层配比", "center", "research", 12, 0, _load_thesis_portfolio),
    Block("quality", "质量观测", "center", "research", 6, 0, _load_quality,
          _SLOW_BLOCK_TIMEOUT_S),
    Block("review", "评测与信号胜率", "center", "research", 6, 0, _load_review),
    Block("storage", "数据中心存储", "left", "research", 12, 0, _load_storage),
    Block("runtime", "运行状态", "left", "system", 12, 0, _load_runtime),
    Block("market", "市场温度", "center", "market", 5, 0, _load_market, _SLOW_BLOCK_TIMEOUT_S),
    Block("shortline", "短线情绪", "center", "market", 7, 0, _load_shortline, _SLOW_BLOCK_TIMEOUT_S),
    Block("portfolio", "持仓概览", "center", "market", 7, 0, _load_portfolio, _SLOW_BLOCK_TIMEOUT_S),
    Block("watchlist", "自选速览", "center", "market", 5, 0, _load_watchlist, _SLOW_BLOCK_TIMEOUT_S),
    Block("strategy_health", "策略健康度", "center", "system", 0, 0, _load_strategy_health, _SLOW_BLOCK_TIMEOUT_S),
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


def _release_thread_conns() -> None:
    """释放**当前线程**的连接缓存 —— 线程局部缓存不会随线程死亡自动回收。

    ⚠️ 为什么必须在这里释放（任务 13 实测，先量后改）：
    _load_block 每装一块都新建一个 ThreadPoolExecutor(1)，线程跑完即死；而
    utils.agent_trace.store 的连接缓存是**线程局部**的（_MAX_CONNS_PER_THREAD 是
    **每线程** LRU）→ 那批连接既不会被 LRU 淘汰，也随线程一起失去引用，永不关闭。
    实测：5 次 activity 强刷 = 5 个线程 × 5 个存在月库 → 连接数 0→25 只增不减；
    再加 3 次 sessions → 40。任务 11 把读 trace 的块从 1 个（旧 usage）变成 3 个
    （usage/activity/sessions），故这是**既有泄漏被放大**。

    只关本线程的连接（store.close_thread_conns 的实现保证不碰其他线程），
    且调用点在 loader **返回之后** —— 此时本线程已不再使用这些连接。
    """
    try:
        from utils.agent_trace.store import store as trace_store
        trace_store.close_thread_conns()
    except Exception:
        # 释放失败绝不能影响块结果（本函数只做收尾，失败即为「没释放」）
        logger.debug("[overview] 释放线程连接缓存失败", exc_info=True)


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

        def _invoke():
            # 在 **worker 线程内**调用 loader 并在返回后释放该线程的连接缓存：
            # 这条线程是本块专用（pool 上限 1、用完即 shutdown），loader 返回后不会再被谁读到。
            # ⚠️ 释放动作必须留在 worker 线程里 —— close_thread_conns 只关「当前线程」的缓存。
            try:
                return block.loader(ctx)
            finally:
                _release_thread_conns()

        try:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                payload = pool.submit(_invoke).result(timeout=block.timeout)
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
