"""拥挤度计算：本地日线 → 四项分项读数 → 0–100 拥挤分。

为什么用「绝对刻度 + 加权合成」而不是池内分位：
板块池会随采集推进而扩张（首轮只覆盖一部分），分位阈值不可复现——今天排 80 分位的
标的，明天可能因为多了 300 个冷门板块而变成 50 分位，读数会莫名其妙地整体漂移。
绝对刻度（延伸度 / 动量 / 放量比 / 追高）可解释、可回归，池子变大也不会集体降压。

取数层（`list_*_subjects` / `read_bars` / `collect_bars` / `load_cursor` / `save_cursor`）
刻意做成实例方法：测试继承覆盖即完全离线（`StubEngine`），与 `thesis/engine.py` 同一套约定。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

from .pool import level_of, load_pool

# 分项 → 指标字段的映射（阈值表的 key 与读数 key 解耦，便于日后换指标）
_METRIC_OF = {"ext": "ext_dev", "mom": "ret20", "vol": "amount_ratio", "chase": "ret60"}

_DEFAULT_WEIGHTS = {"ext": 0.30, "mom": 0.25, "vol": 0.25, "chase": 0.20}


def score_by_table(value, table) -> int | None:
    """按 [下限, 上限, 得分] 表判分（下限含、上限不含）；越界归到首/末档，不给 None。"""
    if value is None or not table:
        return None
    rows = sorted(table, key=lambda r: r[0])
    for lo, hi, sc in rows:
        if lo <= value < hi:
            return sc
    if value < rows[0][0]:
        return rows[0][2]
    return rows[-1][2]


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def compute_metrics(closes: list, amounts: list, ma_days: int, min_rows: int) -> dict | None:
    """由收盘价/成交额序列算四项读数 + 展示项。行数不足或末行缺值返回 None。

    **必须防 None**：`_as_float_list` 会把解析不了的值转成 None（脏日线/空列），
    一个 None 传进除法就是 `TypeError: unsupported operand type(s)`，
    整个作业会以「失败」收场而不是把该标的记成「待补数」。所以这里统一用
    剔除 None 后的序列参与计算（干净数据下与直接用原序列完全等价）。
    """
    n = len(closes)
    if n < min_rows:
        return None
    last = closes[-1]
    if last is None:
        return None
    vals = [c for c in closes if c is not None]
    if not vals:
        return None
    window = vals[-ma_days:] if len(vals) >= ma_days else vals
    ma = _mean(window)
    if not ma:
        return None

    def _ret(lookback: int):
        if n <= lookback:
            return None
        base = closes[-1 - lookback]
        if not base:
            return None
        return round((last / base - 1) * 100, 2)

    # 近 20 日年化波动率（用日收益的标准差，样本标准差）
    vol20 = None
    tail = closes[-21:] if n >= 21 else closes
    rets = []
    for prev, cur in zip(tail, tail[1:]):
        if prev and cur is not None:
            rets.append(cur / prev - 1)
    if len(rets) >= 5:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        vol20 = round(var ** 0.5 * (252 ** 0.5) * 100, 2)

    amount_ratio = None
    amt = [a for a in amounts if a]
    if len(amt) >= 60:
        short = _mean(amt[-5:])
        long = _mean(amt[-60:])
        if short and long:
            amount_ratio = round(short / long, 3)

    return {
        "close": round(float(last), 4),
        "ext_dev": round((last / ma - 1) * 100, 2),
        "ret20": _ret(20),
        "ret60": _ret(60),
        "amount_ratio": amount_ratio,
        "high_gap": round((last / max(window) - 1) * 100, 2),
        "vol20": vol20,
        "ma": round(float(ma), 4),
    }


def is_stale(latest: str | None, stale_days: int, today: date | None = None) -> tuple[bool, int | None]:
    """日线最新日期是否过期。返回 (是否陈旧, 距今天数)。"""
    if not latest:
        return True, None
    try:
        d = datetime.strptime(str(latest)[:10], "%Y-%m-%d").date()
    except ValueError:
        return True, None
    gap = ((today or date.today()) - d).days
    return gap > stale_days, gap


def composite_score(metrics: dict, pool: dict) -> tuple[float | None, dict]:
    """加权合成拥挤分。缺失分项按剩余权重归一化（不用 0 顶替，否则缺数据会被判「舒适」）。"""
    thresholds = pool.get("thresholds") or {}
    weights = pool.get("weights") or _DEFAULT_WEIGHTS
    parts: dict[str, int | None] = {}
    for key, metric_name in _METRIC_OF.items():
        if key in thresholds:
            parts[key] = score_by_table(metrics.get(metric_name), thresholds[key])
    avail = {k: v for k, v in parts.items() if v is not None}
    if not avail:
        return None, parts
    wsum = sum(float(weights.get(k, _DEFAULT_WEIGHTS.get(k, 0))) for k in avail) or 1.0
    total = sum(float(weights.get(k, _DEFAULT_WEIGHTS.get(k, 0))) * v for k, v in avail.items())
    return round(total / wsum, 1), parts


class CrowdingEngine:
    """拥挤度扫描引擎。默认取数走 market_data.db；子类覆盖即离线。"""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("radar.crowding")

    # ── 取数层（实例方法：测试继承覆盖） ─────────────────────────

    def list_index_subjects(self, spec: dict) -> list[dict]:
        """指数池：mode=db 取 index_info 已有指数；include 可追加。"""
        mode = spec.get("mode", "db")
        out: list[dict] = []
        if mode == "db":
            try:
                from utils.cache.market_data_db import get_market_data_db
                with get_market_data_db() as conn:
                    rows = conn.execute(
                        "SELECT code, name FROM index_info ORDER BY code").fetchall()
                out = [{"key": f"index:{r['code']}", "category": "index",
                        "code": r["code"], "name": r["name"] or r["code"]} for r in rows]
            except Exception as e:  # noqa: BLE001
                self.logger.warning("[Crowding] 读 index_info 失败: %s", e)
        for item in (spec.get("include") or []):
            if isinstance(item, str):
                item = {"code": item}
            if not item.get("code"):
                continue
            out.append({"key": f"index:{item['code']}", "category": "index",
                        "code": item["code"], "name": item.get("name") or item["code"]})
        return _cap(_dedup_subjects(out, spec), spec.get("max"))

    def list_board_subjects(self, spec: dict) -> list[dict]:
        """板块池：mode=all 取 stock_board 全量；explicit 只用 include。"""
        if not spec.get("enabled", True):
            return []
        btype = spec.get("board_type", "industry")
        names: list[str] = list(spec.get("include") or [])
        if spec.get("mode", "all") != "explicit":
            try:
                from utils.cache.market_data_db import get_market_data_db
                with get_market_data_db() as conn:
                    rows = conn.execute(
                        "SELECT board_name FROM stock_board WHERE provider = ? AND board_type = ?"
                        " ORDER BY board_name",
                        (spec.get("provider", "akshare"), btype)).fetchall()
                names = list(dict.fromkeys(names + [r["board_name"] for r in rows if r["board_name"]]))
            except Exception as e:  # noqa: BLE001
                self.logger.warning("[Crowding] 读 stock_board 失败: %s", e)
        out = [{"key": f"board:{btype}_{nm}", "category": "board",
                "code": f"board_{btype}_{nm}", "name": nm}
               for nm in names if nm]
        return _cap(_dedup_subjects(out), spec.get("max"))

    def read_bars(self, subject: dict):
        """读标的日线（升序）。无数据返回 None。"""
        try:
            if subject["category"] == "index":
                from utils.cache.market_data_db import get_index_daily
                df = get_index_daily(subject["code"])
            else:
                from utils.cache.market_data_db import get_stock_daily
                df = get_stock_daily(subject["code"])
        except Exception as e:  # noqa: BLE001
            self.logger.warning("[Crowding] 读 %s 日线失败: %s", subject.get("code"), e)
            return None
        if df is None or len(df) == 0:
            return None
        return df

    def collect_bars(self, subject: dict, days: int) -> int:
        """补数（受限流约束，调用方负责限量）。"""
        from utils.cache.market_data_db import (
            fetch_and_store_board_kline, fetch_and_store_index_daily)
        if subject["category"] == "index":
            return fetch_and_store_index_daily(
                subject["code"], name=subject.get("name", ""), days=days)
        return fetch_and_store_board_kline(subject["code"], days=days)

    def load_cursor(self, key: str) -> int:
        from .storage import CrowdingStorage
        return CrowdingStorage.instance().get_cursor(key)

    def save_cursor(self, key: str, value: int) -> None:
        from .storage import CrowdingStorage
        CrowdingStorage.instance().set_cursor(key, value)

    # ── 扫描 ─────────────────────────────────────────────────────

    def scan(self, pool: dict | None = None, collect: bool = True) -> dict:
        """跑一轮：算分 → 按需轮转补数 → （补过数的）重算。返回报告（不落库）。"""
        t0 = time.time()
        warnings: list[str] = []
        if pool is None:
            pool, problems = load_pool()
            warnings.extend(problems)
        params = pool.get("params") or {}
        ma_days = int(params.get("ma_days", 250))
        min_rows = int(params.get("min_rows", 120))
        hist_days = int(params.get("hist_days", 400))
        stale_days = int(params.get("stale_days", 7))
        hot_score = float(params.get("hot_score", 75.0))
        top_n = int(params.get("top_n", 60))

        indices = self.list_index_subjects(pool.get("indices") or {})
        boards = self.list_board_subjects(pool.get("boards") or {})
        subjects = _dedup_subjects(indices + boards)
        if not subjects:
            warnings.append("标的池为空：指数池与板块池都没取到可用标的")

        items: list[dict] = []
        need_collect: list[dict] = []
        for subject in subjects:
            item, needs = self._evaluate(subject, pool, ma_days, min_rows, stale_days)
            items.append(item)
            if needs:
                need_collect.append(subject)

        collected = 0
        if collect and need_collect:
            done, note = self._collect_rotation(
                indices, need_collect, pool, hist_days, min_rows, stale_days)
            collected = len(done)
            if note:
                warnings.append(note)
            for subject in done:
                idx = next((i for i, it in enumerate(items)
                            if it["key"] == subject["key"]), None)
                if idx is None:
                    continue
                item, _needs = self._evaluate(subject, pool, ma_days, min_rows, stale_days)
                items[idx] = item

        items.sort(key=lambda it: (-(it["score"] if it["score"] is not None else -1),
                                   it["status"] != "ok", it["key"]))
        ok_items = [it for it in items if it["status"] == "ok"]
        hot = [it for it in ok_items if (it["score"] or 0) >= hot_score]
        latest_dates = [it["asof"] for it in ok_items if it.get("asof")]
        trade_date = max(latest_dates) if latest_dates else date.today().strftime("%Y-%m-%d")

        report = {
            "trade_date": trade_date,
            "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pool_size": len(subjects),
            "computed_count": len(ok_items),
            "insufficient_count": sum(1 for it in items if it["status"] == "insufficient"),
            "stale_count": sum(1 for it in items if it["status"] == "stale"),
            "hot_count": len(hot),
            "collected_count": collected,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "pool": {"indices": len(indices), "boards": len(boards)},
            "params": params,
            "weights": pool.get("weights") or _DEFAULT_WEIGHTS,
            "labels": pool.get("labels") or {},
            "levels": pool.get("levels") or [],
            "hot_score": hot_score,
            "items": items,
            "hot": hot[:top_n],
            "top": ok_items[:top_n],
            "warnings": warnings,
        }
        return report

    # ── 内部 ─────────────────────────────────────────────────────

    def _evaluate(self, subject: dict, pool: dict, ma_days: int, min_rows: int,
                  stale_days: int) -> tuple[dict, bool]:
        """对单个标的算分。返回 (明细项, 是否需要补数)。"""
        df = self.read_bars(subject)
        rows = len(df) if df is not None else 0
        asof = None
        if rows:
            try:
                asof = str(df.iloc[-1]["trade_date"])[:10]
            except Exception:  # noqa: BLE001
                asof = None
        stale, gap = is_stale(asof, stale_days)

        base = {
            "key": subject["key"], "category": subject["category"],
            "code": subject["code"], "name": subject.get("name") or subject["code"],
            "rows": rows, "asof": asof, "stale_days": gap,
            "close": None, "ext_dev": None, "ret20": None, "ret60": None,
            "amount_ratio": None, "high_gap": None, "vol20": None,
            "score": None, "level": None, "parts": {},
        }
        if rows < min_rows:
            base["status"] = "insufficient"
            base["detail"] = (f"日线仅 {rows} 行（需 ≥{min_rows}）" if rows
                              else "本地无日线数据，待补数")
            return base, True

        closes = _as_float_list(df, "close")
        amounts = _as_float_list(df, "amount")
        metrics = compute_metrics(closes, amounts, ma_days, min_rows)
        if metrics is None:
            base["status"] = "insufficient"
            base["detail"] = f"日线仅 {rows} 行（需 ≥{min_rows}）"
            return base, True

        base.update(metrics)
        if stale:
            base["status"] = "stale"
            base["level"] = None
            base["detail"] = (f"日线停在 {asof}（距今 {gap} 天，> {stale_days} 天），"
                              "读数不可用，待补数")
            return base, True

        score, parts = composite_score(metrics, pool)
        base["score"] = score
        base["parts"] = parts
        base["level"] = level_of(score, pool.get("levels") or [])
        base["status"] = "ok" if score is not None else "insufficient"
        base["detail"] = _describe(metrics, base["level"], parts)
        return base, False

    def _collect_rotation(self, indices: list[dict], need: list[dict], pool: dict,
                          hist_days: int, min_rows: int, stale_days: int) -> tuple[list[dict], str | None]:
        """补数：指数全补（池小），板块按游标分批（每次调用有 2–5s 上游限流）。"""
        index_keys = {s["key"] for s in indices}
        must = [s for s in need if s["key"] in index_keys]
        board_need = [s for s in need if s["key"] not in index_keys]

        limit = _collect_limit(pool)
        note = None
        if limit > 0 and len(board_need) > limit:
            total = len(board_need)
            try:
                offset = self.load_cursor("boards") % total
            except Exception:  # noqa: BLE001
                offset = 0
            picked = (board_need + board_need)[offset:offset + limit]
            note = (f"板块池有 {total} 个待补数，本轮先补 {len(picked)} 个"
                    f"（上游限流，按游标分批推进）")
            board_need = picked
            try:
                # 下次从本轮末尾续接（游标落在「待补列表」的逻辑位置上，越过 total 自动回绕）
                self.save_cursor("boards", (offset + limit) % total)
            except Exception:  # noqa: BLE001
                pass

        done: list[dict] = []
        for subject in must + board_need:
            try:
                self.collect_bars(subject, hist_days)
                done.append(subject)
            except Exception as e:  # noqa: BLE001
                self.logger.warning("[Crowding] 补数失败 %s: %s", subject.get("code"), e)
        return done, note


def _describe(metrics: dict, level: str | None, parts: dict) -> str:
    bits = [f"较 250 日均线 {metrics['ext_dev']:+.2f}%"]
    if metrics.get("ret20") is not None:
        bits.append(f"20 日 {metrics['ret20']:+.2f}%")
    if metrics.get("ret60") is not None:
        bits.append(f"60 日 {metrics['ret60']:+.2f}%")
    if metrics.get("amount_ratio") is not None:
        bits.append(f"放量比 {metrics['amount_ratio']:.2f}")
    head = f"{level}｜" if level else ""
    return head + "；".join(bits)


def _as_float_list(df, column: str) -> list:
    if column not in df.columns:
        return []
    out = []
    for v in df[column].tolist():
        try:
            out.append(None if v is None else float(v))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _dedup_subjects(subjects: list[dict], spec: dict | None = None) -> list[dict]:
    seen, out = set(), []
    for s in subjects:
        if not s or not s.get("code") or s["key"] in seen:
            continue
        seen.add(s["key"])
        out.append(s)
    return out


def _cap(subjects: list[dict], max_n) -> list[dict]:
    try:
        n = int(max_n or 0)
    except (TypeError, ValueError):
        n = 0
    return subjects[:n] if n > 0 else subjects


def _collect_limit(pool: dict) -> int:
    params = pool.get("params") or {}
    boards = pool.get("boards") or {}
    raw = params.get("collect_limit_per_run") or boards.get("collect_limit_per_run") or 150
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 150
