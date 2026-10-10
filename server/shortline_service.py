"""短线情绪观测聚合服务（P0/P1）。

设计：docs/superpowers/specs/2026-09-10-short-line-monitor-design.md
- 数据源：同花顺官方 REST（tools/fetcher/hithink_ds），4 池 + 天梯 + 批量行情；
- 限流约束（实测）：连续无间隔请求 4~5 次触发 429 → 串行 + 步间 sleep + 429 等待重试；
- 降级：单池失败置 None + degraded 标记，整体不抛错；
- 存储：仅固化每日情绪温度快照（shortline_daily，shortline.db），走统一 db_schema。

纯函数（可单测，不触网）：compute_promotion / aggregate_themes / merge_ladder /
compute_summary / premium_stats。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("radar.shortline")

# ── 拉取节流（429 实测约束） ────────────────────────────────────────

_STEP_SLEEP = 2.0        # 相邻端点请求间隔（秒）
_RETRY_SLEEP = 30.0      # 429 等待重试间隔（秒）
_MAX_RETRIES = 2
_OVERVIEW_TTL = 60.0     # overview 结果缓存（秒）
_UPSTREAM_COOLDOWN = 180.0  # 上游连续失败后的冷却（秒）：期间直接走快照，避免每次空等
_FETCH_TIMEOUT = 5.0     # 短线聚合单请求超时覆盖（默认 15s 太慢，全败要等 60s+）

_overview_cache: dict[str, tuple[float, dict]] = {}
_fetch_lock = threading.Lock()  # 串行化拉取，防并发面板刷新打爆配额
_upstream_down_until = 0.0      # 上游不可达冷却截止时间戳（0 = 正常）


def _get_hithink():
    from tools.fetcher.hithink_ds import HithinkDataSource
    return HithinkDataSource


def _call_with_429_retry(fn, *args, **kwargs):
    """429 限流等待重试；其他异常原样抛（由调用方降级）。"""
    import requests
    last = None
    for i in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and i < _MAX_RETRIES:
                logger.warning("[shortline] 429 限流，等待 %.0fs 重试 (%d/%d)", _RETRY_SLEEP, i + 1, _MAX_RETRIES)
                time.sleep(_RETRY_SLEEP)
                last = e
                continue
            raise
    raise last


def _fetch_pools(date: str | None) -> dict:
    """串行拉取各数据池（每步 sleep 节流），单池失败降级为空并记录 degraded。"""
    H = _get_hithink()
    degraded: list[str] = []
    out: dict[str, Any] = {
        "limit_up": [], "limit_down": [], "broken": [], "ladder": [],
        "degraded": degraded,
    }

    def _step(name: str, fn):
        try:
            return _call_with_429_retry(fn)
        except Exception as e:
            logger.warning("[shortline] %s 拉取失败（降级）: %s", name, e)
            degraded.append(name)
            return None

    with _fetch_lock:
        prev_timeout = H._REQUEST_TIMEOUT_OVERRIDE
        H._REQUEST_TIMEOUT_OVERRIDE = _FETCH_TIMEOUT
        try:
            out["ladder"] = _step("ladder", lambda: H.get_lianban_ladder(days=6)) or []
            time.sleep(_STEP_SLEEP)
            out["limit_up"] = _step("limit_up", lambda: H.get_limit_up_pool(date=date, n=200)) or []
            time.sleep(_STEP_SLEEP)
            out["broken"] = _step("broken", lambda: H.get_limit_break_pool(date=date, n=200)) or []
            time.sleep(_STEP_SLEEP)
            out["limit_down"] = _step("limit_down", lambda: H.get_limit_down_pool(date=date, n=200)) or []
        finally:
            H._REQUEST_TIMEOUT_OVERRIDE = prev_timeout
    return out


# ── 纯函数聚合 ──────────────────────────────────────────────────────


def _parse_reasons(reason: str) -> list[str]:
    """复合题材标签切分："800G光引擎+CPO+AI算力" → 3 个题材（去空去重保序）。"""
    if not reason:
        return []
    parts = [p.strip() for p in str(reason).replace("＋", "+").split("+")]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def compute_promotion(ladder: list) -> list[dict]:
    """晋级率矩阵：相邻交易日 diff（昨日 k 板 → 今日 k+1 板命中率）。

    ladder 按日期倒序（[0] 最新）。返回 [{"node": "1->2", "hit", "base", "rate"}, ...]。
    注：天梯仅含 ≥2 板；一进二以"昨日首板"无法从天梯获得 → 用昨日涨停池总数近似
    （base 由调用方传入的可选池数据补，缺省时跳过 1->2）。
    """
    if len(ladder) < 2:
        return []
    today_codes_by_board: dict[int, set] = {}
    for b in ladder[0].get("boards", []):
        today_codes_by_board.setdefault(int(b.get("board_num") or 0), set()).add(b.get("code"))
    yest_codes_by_board: dict[int, set] = {}
    for b in ladder[1].get("boards", []):
        yest_codes_by_board.setdefault(int(b.get("board_num") or 0), set()).add(b.get("code"))

    rows = []
    for k in sorted(yest_codes_by_board):
        base_codes = yest_codes_by_board[k]
        if not base_codes:
            continue
        hit = len(base_codes & today_codes_by_board.get(k + 1, set()))
        rows.append({
            "node": f"{k}->{k+1}",
            "hit": hit,
            "base": len(base_codes),
            "rate": round(hit / len(base_codes), 3),
        })
    return rows


def merge_ladder(ladder: list, pool: list) -> list:
    """天梯（≥2板）合并涨停池字段：题材/封单/首封时间按 code 回填。

    返回近 6 日结构；最新日（[0]）字段最全，历史日仅复盘用（seal_nextday）。
    """
    pool_by_code = {str(p.get("code", "")).strip(): p for p in pool}
    out = []
    for day in ladder or []:
        boards = []
        for b in day.get("boards", []):
            code = str(b.get("code", "")).strip()
            p = pool_by_code.get(code) or {}
            reasons = _parse_reasons(p.get("limit_up_reason", ""))
            boards.append({
                "code": code,
                "name": b.get("name", ""),
                "board_num": int(b.get("board_num") or 0),
                "seal_nextday": b.get("seal_nextday"),
                "sign_level": int(b.get("sign_level") or 0),
                "theme": reasons[0] if reasons else "",
                "themes": reasons,
                "seal_amount": p.get("seal_amount"),
                "first_time": p.get("limit_up_time") or "",
            })
        out.append({"date": day.get("date", ""), "boards": boards})
    return out


def aggregate_themes(pool: list) -> list[dict]:
    """题材聚合：limit_up_reason 按 "+" 切分归组（一票多题材，计数可重叠）。

    返回按（最高板降序, 数量降序）排序的题材列表。
    """
    agg: dict[str, dict] = {}
    for p in pool or []:
        board = int(p.get("consecutive_boards") or 0)
        for t in _parse_reasons(p.get("limit_up_reason", "")):
            item = agg.setdefault(t, {"name": t, "count": 0, "max_board": 0, "codes": []})
            item["count"] += 1
            item["max_board"] = max(item["max_board"], board)
            item["codes"].append(str(p.get("code", "")).strip())
    rows = sorted(agg.values(), key=lambda x: (-(x["max_board"] * 3 + x["count"]), -x["max_board"], x["name"]))
    for r in rows:
        r["ladder_ok"] = r["max_board"] >= 2 and r["count"] >= 2
    return rows


def compute_summary(pool: list, limit_down: list, broken: list, ladder: list,
                    promo: list) -> dict:
    """情绪温度计（四因子 25/30/30/15，见 spec §3）。"""
    limit_up_count = len(pool or [])
    limit_down_count = len(limit_down or [])
    broken_count = len(broken or [])
    boards = [int(p.get("consecutive_boards") or 0) for p in pool or []]
    max_board = max(boards) if boards else 0
    if not max_board and ladder:
        for b in ladder[0].get("boards", []):
            max_board = max(max_board, int(b.get("board_num") or 0))

    denom = limit_up_count + broken_count
    break_rate = round(broken_count / denom, 3) if denom else 0.0

    tot_hit = sum(r["hit"] for r in promo or [])
    tot_base = sum(r["base"] for r in promo or [])
    promotion_rate = round(tot_hit / tot_base, 3) if tot_base else 0.0

    f_height = min(max_board / 7, 1.0)
    f_promo = min(promotion_rate / 0.6, 1.0)
    # 炸板率 ≤20% 满格、≥40% 归零（线性）；无涨停无炸板（非交易日/全失败）不给信用分
    f_break = max(0.0, min((0.40 - break_rate) / 0.20, 1.0)) if denom else 0.0
    f_ratio = max(0.0, min((limit_up_count - 5 * limit_down_count) / 100, 1.0))

    score = round(25 * f_height + 30 * f_promo + 30 * f_break + 15 * f_ratio)
    level = "ice" if score < 30 else ("ebb" if score < 50 else ("warm" if score <= 70 else "hot"))
    return {
        "score": score, "level": level,
        "max_board": max_board,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "broken_count": broken_count,
        "break_rate": break_rate,
        "promotion_rate": promotion_rate,
    }


def premium_stats(ladder: list, quotes_pct: list[float]) -> dict:
    """昨日梯队股今日溢价统计（均值/中位数）。"""
    vals = [float(v) for v in quotes_pct if v is not None]
    if not vals:
        return {"mean": None, "median": None, "n": 0}
    vals.sort()
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"mean": round(sum(vals) / n, 2), "median": round(mid, 2), "n": n}


# ── 存储（shortline.db，走统一 schema） ─────────────────────────────


def _connect() -> sqlite3.Connection:
    from utils.app_paths import get_shortline_db_path
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    conn = sqlite3.connect(get_shortline_db_path())
    conn.row_factory = sqlite3.Row
    d = ALL_DBS["shortline"]
    ensure_schema(conn, d.tables, d.special_migrations)
    return conn


def upsert_daily(summary: dict, promo: list, themes: list, premium: dict,
                 payload: dict | None = None) -> None:
    """固化当日情绪快照（(trade_date) 幂等；payload = 完整 overview，用于上游故障回退）。"""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        top_theme = themes[0]["name"] if themes else ""
        conn.execute(
            """INSERT INTO shortline_daily
               (trade_date, score, level, max_board, limit_up_count, limit_down_count,
                broken_count, break_rate, promotion_rate, promo_detail, premium_mean,
                premium_median, top_theme, theme_detail, snapshot_json, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hithink', ?, ?)
               ON CONFLICT(trade_date) DO UPDATE SET
                 score=excluded.score, level=excluded.level, max_board=excluded.max_board,
                 limit_up_count=excluded.limit_up_count, limit_down_count=excluded.limit_down_count,
                 broken_count=excluded.broken_count, break_rate=excluded.break_rate,
                 promotion_rate=excluded.promotion_rate, promo_detail=excluded.promo_detail,
                 premium_mean=excluded.premium_mean, premium_median=excluded.premium_median,
                 top_theme=excluded.top_theme, theme_detail=excluded.theme_detail,
                 snapshot_json=COALESCE(excluded.snapshot_json, snapshot_json),
                 updated_at=excluded.updated_at""",
            (summary["trade_date"], summary["score"], summary["level"], summary["max_board"],
             summary["limit_up_count"], summary["limit_down_count"], summary["broken_count"],
             summary["break_rate"], summary["promotion_rate"],
             json.dumps(promo, ensure_ascii=False),
             premium.get("mean"), premium.get("median"), top_theme,
             json.dumps([{k: t[k] for k in ("name", "count", "max_board")} for t in themes[:20]],
                        ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False) if payload else None,
             now, now))
        conn.commit()
    finally:
        conn.close()


def _load_latest_snapshot() -> dict | None:
    """读取最近一份完整快照（上游不可达/非交易日时回退展示）。"""
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT snapshot_json, trade_date FROM shortline_daily "
                "WHERE snapshot_json IS NOT NULL ORDER BY trade_date DESC LIMIT 1").fetchone()
            if not row or not row["snapshot_json"]:
                return None
            data = json.loads(row["snapshot_json"])
            data["stale"] = True
            data["stale_date"] = row["trade_date"]
            return data
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[shortline] 读取历史快照失败: %s", e)
        return None


def _empty_response(degraded: list, error: str) -> dict:
    """空响应壳（上游不可达且无快照可回退）：结构对齐正常响应，前端显示空态提示。"""
    return {
        "summary": None, "ladder": [], "themes": [], "promo": [],
        "quality": {"top_seal": [], "broken": [], "down": []},
        "first_boards": [], "degraded": degraded, "error": error,
    }


def get_history(days: int = 30) -> list[dict]:
    """情绪温度历史（近 N 个交易日，正序）。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT trade_date, score, level, max_board, limit_up_count, limit_down_count, "
            "broken_count, break_rate, promotion_rate, premium_mean, top_theme "
            "FROM shortline_daily ORDER BY trade_date DESC LIMIT ?", (int(days),)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


# ── 主编排 ──────────────────────────────────────────────────────────


def build_overview(date: str | None = None, *, use_cache: bool = True) -> dict:
    """聚合整页数据（60s 缓存；单池失败降级不抛错；上游整体不可达回退最近快照）。"""
    global _upstream_down_until
    cache_key = date or "today"
    now = time.time()
    if use_cache:
        hit = _overview_cache.get(cache_key)
        if hit and now - hit[0] < _OVERVIEW_TTL:
            return hit[1]

    # 上游冷却期内：有快照走快照；无快照也快速返回空（不再触网空等）
    if now < _upstream_down_until:
        snap = _load_latest_snapshot()
        if snap:
            if use_cache:
                _overview_cache[cache_key] = (now, snap)
            return snap
        return _empty_response([], "上游数据源冷却中，且无历史快照可回退")

    pools = _fetch_pools(date)

    # 四池全败 → 判定上游不可达：冷却 + 回退最近快照（非交易日/服务抖动场景）
    if len(pools["degraded"]) >= 4:
        _upstream_down_until = time.time() + _UPSTREAM_COOLDOWN
        snap = _load_latest_snapshot()
        if snap:
            logger.warning("[shortline] 上游不可达，回退最近快照 %s（冷却 %.0fs）",
                           snap.get("stale_date"), _UPSTREAM_COOLDOWN)
            if use_cache:
                _overview_cache[cache_key] = (now, snap)
            return snap
        # 无快照可回退：返回空响应，不写假快照污染情绪历史
        logger.warning("[shortline] 上游不可达且无历史快照，返回空响应（冷却 %.0fs）", _UPSTREAM_COOLDOWN)
        return _empty_response(pools["degraded"], "上游数据源不可达，且无历史快照可回退")

    ladder_raw = pools["ladder"]
    pool = pools["limit_up"]
    ladder = merge_ladder(ladder_raw, pool)
    promo = compute_promotion(ladder_raw)

    # 昨日梯队 → 今日溢价（ladder[1] 的 ≥2 板股；批量行情一次）
    premium = {"mean": None, "median": None, "n": 0}
    if len(ladder_raw) >= 2:
        y_codes = [b.get("code") for b in ladder_raw[1].get("boards", []) if b.get("code")]
        if y_codes:
            try:
                H = _get_hithink()
                q = _call_with_429_retry(H.fetch_quotes, y_codes[:100])
                if q is not None and not q.empty and "涨跌幅" in q.columns:
                    premium = premium_stats(ladder_raw, q["涨跌幅"].dropna().tolist())
            except Exception as e:
                logger.warning("[shortline] 昨日溢价行情失败（降级）: %s", e)
                pools["degraded"].append("premium")

    themes = aggregate_themes(pool)
    summary = compute_summary(pool, pools["limit_down"], pools["broken"], ladder_raw, promo)

    # 交易日归属：以天梯最新日为锚（涨停池空说明非交易日）
    trade_date = (ladder[0]["date"] if ladder else "") or (date or datetime.now().strftime("%Y-%m-%d"))
    summary["trade_date"] = trade_date
    summary["yesterday_premium"] = premium

    quality = {
        "top_seal": sorted(
            [{"code": p.get("code"), "name": p.get("name"), "seal_amount": p.get("seal_amount"),
              "first_time": p.get("limit_up_time"), "theme": (_parse_reasons(p.get("limit_up_reason", "")) or [""])[0]}
             for p in pool if p.get("seal_amount")],
            key=lambda x: -(x["seal_amount"] or 0))[:10],
        "broken": [{"code": b.get("code"), "name": b.get("name"),
                    "open_times": b.get("open_times"), "amount": b.get("amount")}
                   for b in pools["broken"]],
        "down": [{"code": d.get("code"), "name": d.get("name"),
                  "last_limit_time": d.get("last_limit_time")} for d in pools["limit_down"]],
    }

    # 首板墙（涨停池中连板数=1）
    first_boards = [{
        "code": p.get("code"), "name": p.get("name"),
        "theme": (_parse_reasons(p.get("limit_up_reason", "")) or [""])[0],
        "seal_amount": p.get("seal_amount"), "first_time": p.get("limit_up_time"),
    } for p in pool if int(p.get("consecutive_boards") or 0) <= 1]

    result = {
        "summary": summary,
        "ladder": ladder,
        "themes": themes[:30],
        "promo": promo,
        "quality": quality,
        "first_boards": first_boards[:80],
        "degraded": pools["degraded"],
    }

    # 固化快照（含完整 payload，供上游故障回退；失败不影响响应）
    try:
        upsert_daily(summary, promo, themes, premium, payload=result)
    except Exception as e:
        logger.warning("[shortline] 快照落库失败: %s", e)

    if use_cache:
        _overview_cache[cache_key] = (now, result)
    return result
