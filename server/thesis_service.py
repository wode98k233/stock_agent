"""研究台账 Web 服务：刷新（计算 → 落定义 → 落快照 → 产告警）与读取。

放在 `server/` 而非 `thesis/` 的理由：这一层是 Web 编排（含降级话术、给前端的形状），
而 `thesis/` 包保持纯领域（engine/storage/alerts 都不依赖 FastAPI），
CLI 与技能可直接用领域层，不必绕 Web。

所有函数对「部分失败」保持宽容：单个台账坏定义不影响列表页，取数失败也返回降级形状
而不是 500——台账页最怕白屏。
"""
import logging
import math
import time

logger = logging.getLogger("radar.thesis")


def _default_engine():
    from thesis.engine import ThesisEngine
    return ThesisEngine()


def _default_storage():
    from thesis.storage import ThesisStorage
    return ThesisStorage.instance()


# ── 刷新 ──────────────────────────────────────────────────────────────

def refresh(thesis_id: str, engine=None, storage=None, trade_date: str | None = None) -> dict:
    """重算并落库。返回 {snapshot, alerts, new_alert_count, definition_changed}。

    定义不可用时抛 ValueError，由路由层转成友好错误（这是配置问题，不是取数问题，
    必须让用户看见，不能降级成"数据缺失"）。

    `trade_date` 不传时按运行日（最近已完成交易日）归档；显式传入仅用于测试/补录。
    """
    from thesis import alerts as thesis_alerts
    from thesis import defs as thesis_defs

    engine = engine or _default_engine()
    storage = storage or _default_storage()

    defn, problems = thesis_defs.load_def_checked(thesis_id)
    if defn is None:
        raise ValueError("台账定义不可用：" + "；".join(problems))

    result = engine.compute(thesis_id, defn)
    def_hash, def_is_new = storage.save_definition(thesis_id, defn)
    result["def_hash"] = def_hash

    # 先按同一口径算归档日期，再取「上一条」快照做差分——顺序反了会拿自己跟自己比
    trade_date = trade_date or storage.resolve_trade_date(result)
    prev = storage.latest_snapshot(thesis_id, before_date=trade_date)
    saved = storage.save_snapshot(result, trade_date=trade_date)
    cur = storage.get_snapshot(thesis_id, saved["trade_date"])

    cur_alerts = thesis_alerts.diff_snapshots(prev, cur) if cur else []
    added = storage.save_alerts(cur_alerts)

    # 台账数据已更新 → 清掉该台账的因子证据 / 报告缓存：下次点开「数据验证」
    # 或「生成报告」会用最新数据重算，而不是返回旧结论。
    invalidate_evidence_cache(thesis_id)
    invalidate_report_cache(thesis_id)
    # 双轴图**只在「定义或数据真的变了」时清**：1h 缓存的意义就是同一版本、
    # 同一数据重复刷新不重算；启动巡检/同一天覆盖快照（created=False）不该把
    # 预热脚本写好的 L2 清掉（否则每次重启都白预热）。L3 因子缓存永远保留：
    # 新数据会通过 input_end 前进自动失效，这正是「有新数据只算新增/必要部分」。
    if def_is_new or saved.get("created"):
        invalidate_chart_cache(thesis_id)

    if problems:
        logger.warning("[Thesis] %s 定义存在问题: %s", thesis_id, problems)
    logger.info("[Thesis] %s 刷新完成 date=%s total=%s 告警 +%d（定义%s）",
                thesis_id, saved["trade_date"], result.get("total"), added,
                "已变更" if def_is_new else "未变")

    return {
        "snapshot": cur,
        "alerts": cur_alerts,
        "new_alert_count": added,
        "definition_changed": def_is_new,
        "definition_problems": problems,
        # 是否新增了一个历史点（False = 同一批数据重跑，只覆盖不新增）
        "snapshot_created": saved["created"],
    }


# ── 读取 ──────────────────────────────────────────────────────────────

def list_theses(storage=None) -> list[dict]:
    """台账列表：定义目录里有什么 + 各自最新快照状态。

    定义来自文件系统（可能从未跑过），快照来自库（可能为空），两边做外连接——
    只有这样「新建了台账但还没刷新」才不会从列表里消失。
    """
    from thesis import defs as thesis_defs

    storage = storage or _default_storage()
    snapshots = {row["thesis_id"]: row for row in storage.list_snapshot_ids()}
    items = []
    for tid in thesis_defs.list_defs():
        defn, problems = thesis_defs.load_def_checked(tid)
        snap = snapshots.get(tid) or {}
        items.append({
            "thesis_id": tid,
            "name": (defn or {}).get("name", tid),
            "asset_label": (defn or {}).get("asset_label", ""),
            "description": (defn or {}).get("description", ""),
            "definition_version": (defn or {}).get("version"),
            "definition_ok": not problems,
            "definition_problems": problems,
            "subject": (defn or {}).get("subject", {}),
            "has_snapshot": bool(snap),
            "trade_date": snap.get("trade_date"),
            "total": snap.get("total"),
            "valid_count": snap.get("valid_count"),
            "factor_count": snap.get("factor_count"),
            "verdict": snap.get("verdict"),
            "action": snap.get("action"),
            "snapshot_count": snap.get("snapshot_count", 0),
        })
    return items


def get_detail(thesis_id: str, storage=None) -> dict:
    """台账详情：最新快照 + 定义元信息 + 最近告警 + 定义变更历史。"""
    from thesis import alerts as thesis_alerts
    from thesis import defs as thesis_defs

    storage = storage or _default_storage()
    defn, problems = thesis_defs.load_def_checked(thesis_id)
    snapshot = storage.latest_snapshot(thesis_id)
    recent_alerts = storage.list_alerts(thesis_id, days=30, limit=100)
    return {
        "thesis_id": thesis_id,
        "name": (defn or {}).get("name", thesis_id),
        "asset_label": (defn or {}).get("asset_label", ""),
        "definition": defn or {},
        "definition_ok": not problems,
        "definition_problems": problems,
        "definition_history": storage.get_definition_history(thesis_id, limit=20),
        "snapshot": snapshot,
        "alerts": recent_alerts,
        "alert_summary": thesis_alerts.summarize(recent_alerts),
    }


def get_history(thesis_id: str, days: int = 90, storage=None) -> dict:
    storage = storage or _default_storage()
    items = storage.get_history(thesis_id, days=days)
    return {"thesis_id": thesis_id, "items": items, "count": len(items)}


def get_alerts(thesis_id: str | None = None, days: int = 30, storage=None) -> list[dict]:
    storage = storage or _default_storage()
    return storage.list_alerts(thesis_id, days=days, limit=300)


# ── 组合层：verdict → 建议权重区间 ─────────────────────────────────────

def load_portfolio_map() -> dict | None:
    """读组合映射表 `data/portfolio_map.json`（带 mtime 缓存）。

    刻意**不放在 `data/thesis/`**：那个目录下所有 `*.json` 都会被
    `thesis_defs.list_defs()` 当成台账定义加载，放进去会多出一个缺 subject/thresholds
    的「假台账」，列表页与校验都会报警。
    """
    import json
    import os
    from pathlib import Path

    from utils.app_paths import get_data_path
    path = Path(get_data_path()) / "portfolio_map.json"
    if not path.exists():
        return None
    try:
        mtime = os.path.getmtime(path)
        cached = getattr(load_portfolio_map, "_cache", None)
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        load_portfolio_map._cache = (mtime, data)
        return data
    except Exception as e:            # 坏 JSON 不该让组合卡片 500
        logger.warning("[Thesis] 组合映射表解析失败: %s", e)
        return None


def load_portfolios() -> list[dict]:
    """组合定义列表。兼容两种文件格式：

    - **多组合（v2）**：`{"version": "...", "portfolios": [ {...}, {...} ]}`
    - **单组合（v1）**：顶层即一个组合定义（`id`/`legs`/`bands`…）

    兼容是刻意的：v1 文件（现行 `dumbbell_div_hstech`）不迁移也能继续跑，
    v2 只是把「一个组合」放开成「多个组合」，不需要一次性改数据。
    """
    d = load_portfolio_map()
    if not d:
        return []
    pfs = d.get("portfolios")
    if isinstance(pfs, list):
        return [p for p in pfs if isinstance(p, dict)]
    return [d]


def get_portfolio_suggestion(storage=None, portfolio_id: str | None = None) -> dict:
    """把台账总分映射成组合建议权重区间（支持多组合 / 多驱动 / 共同风险）。

    映射表缺失、或驱动腿还没有快照时返回 `{ok: False, reason}`——**不猜权重**：
    没有环境判断就没有配比结论，编一个默认值比不显示更危险。
    """
    portfolios = load_portfolios()
    if not portfolios:
        return {"ok": False, "reason": "未找到组合映射表（data/portfolio_map.json）"}
    pf = portfolios[0]
    if portfolio_id:
        found = next((p for p in portfolios if p.get("id") == portfolio_id), None)
        if found is None:
            return {"ok": False, "reason": f"未找到组合 {portfolio_id}",
                    "available": [p.get("id") for p in portfolios]}
        pf = found
    storage = storage or _default_storage()

    legs = []
    for leg in pf.get("legs") or []:
        tid = leg.get("thesis_id")
        snap = None
        try:
            snap = storage.latest_snapshot(tid)
        except Exception as e:
            logger.warning("[Thesis] 组合映射读取 %s 快照失败: %s", tid, e)
        legs.append({
            "thesis_id": tid, "label": leg.get("label"), "role": leg.get("role"),
            "total": (snap or {}).get("total"), "verdict": (snap or {}).get("verdict"),
            "trade_date": (snap or {}).get("trade_date"),
        })
    totals = {x["thesis_id"]: x.get("total") for x in legs}

    # drivers：v2 多驱动列表；缺省回退 v1 的单个 driver 字段
    drivers = pf.get("drivers") or ([pf["driver"]] if pf.get("driver") else [])
    driver_ids = [d for d in drivers if d in totals]
    if not driver_ids:
        return {"ok": False, "id": pf.get("id"), "name": pf.get("name"), "legs": legs,
                "reason": "组合未声明驱动腿（drivers/driver）"}
    missing = [d for d in driver_ids if totals.get(d) is None]
    if missing:
        return {"ok": False, "id": pf.get("id"), "name": pf.get("name"), "legs": legs,
                "drivers": driver_ids,
                "reason": "驱动腿（%s）暂无快照，先刷新台账" % "、".join(missing)}

    band = None
    for b in pf.get("bands") or []:      # 按书写顺序取首个满足者（须降序书写）
        if _band_matches(b, totals, totals.get(driver_ids[0])):
            band = b
            break

    # 共同风险（观察①的落点）：整体降仓信号，与 bands 是两件事
    risks = _eval_co_exposure(pf, totals)

    # 组合风险：腿间相关性 + 等权组合波动 vs 两腿平均（把 basis 里
    # 写死的结论性文案变成可复算的读数）
    risk = _portfolio_risk(pf.get("legs") or [])

    # 档位全景：全部档位 + 当前档 + 距下一档差多少（回答「还要涨几分才换档」）
    outline = _bands_outline(pf.get("bands") or [], totals, driver_ids[0])

    # 组合级总分：驱动腿加权（等权）——仅作展示读数，不参与定档
    drv_scores = [totals[d] for d in driver_ids if totals.get(d) is not None]
    combo_total = round(sum(drv_scores) / len(drv_scores), 2) if drv_scores else None

    result = {"ok": True, "id": pf.get("id"), "name": pf.get("name"),
              "basis": pf.get("basis"), "legs": legs, "band": band,
              "drivers": driver_ids, "combo_total": combo_total,
              "note": pf.get("_说明"), "co_exposure": pf.get("co_exposure"),
              "risks": risks, "risk": risk, "bands_outline": outline,
              "portfolio_count": len(portfolios),
              "available": [p.get("id") for p in portfolios]}
    # v1 兼容：保留 driver 单数字段
    if len(driver_ids) == 1:
        result["driver"] = driver_ids[0]
    return result


def _portfolio_risk(legs: list[dict]) -> dict:
    """腿间相关性 + 波动结构（本地日线自算，最近 250 个共同交易日）。

    存在理由：`portfolio_map.json` 的 basis 里可能写入腿间相关性、波动摊薄这类
    **结论性常量**——写死之后没人能验证它们是不是还成立（相关性会漂）。这里把同一
    口径复算出来，让 basis 从「断言」变成「可复检的读数」。

    口径：日收益年化波动（×√252），等权组合 vs 两腿平均。
    """
    import numpy as np

    from utils.cache.market_data_db import get_index_daily

    picked = []
    for leg in legs:
        tgt = _EVIDENCE_TARGET.get(leg.get("thesis_id"))
        if tgt:
            picked.append((leg.get("thesis_id"), leg.get("label") or leg.get("thesis_id"), tgt[0]))
    if len(picked) < 2:
        return {"available": False, "reason": "可投指数不足 2 条腿，无法算组合结构"}

    series: dict[str, dict] = {}
    for tid, label, code in picked:
        try:
            df = get_index_daily(code)
        except Exception as e:
            logger.warning("[Thesis] 组合风险取 %s 日线失败: %s", code, e)
            continue
        if df is None or len(df) < 60:
            continue
        series[tid] = {"label": label, "code": code,
                       "map": dict(zip([str(x)[:10] for x in df["trade_date"].tolist()],
                                       [float(x) for x in df["close"].tolist()]))}
    if len(series) < 2:
        return {"available": False, "reason": "腿的日线不足（<60 根），无法算组合结构"}

    common = sorted(set.intersection(*[set(v["map"]) for v in series.values()]))
    if len(common) < 60:
        return {"available": False, "reason": "共同交易日不足（%d 天）" % len(common)}
    common = common[-250:]

    rets = {}
    for tid, v in series.items():
        px = np.array([v["map"][d] for d in common], dtype=float)
        rets[tid] = np.diff(px) / px[:-1]

    keys = list(rets)
    vols = {k: float(np.std(rets[k]) * np.sqrt(252) * 100) for k in keys}
    corr = None
    if len(keys) == 2:
        corr = float(np.corrcoef(rets[keys[0]], rets[keys[1]])[0, 1])
    ew = np.mean([rets[k] for k in keys], axis=0)
    ew_vol = float(np.std(ew) * np.sqrt(252) * 100)
    avg_vol = float(np.mean(list(vols.values())))
    return {
        "available": True, "window": len(common),
        "corr": round(corr, 2) if corr is not None else None,
        "leg_vols": [[series[k]["label"], round(vols[k], 1)] for k in keys],
        "equal_weight_vol": round(ew_vol, 1),
        "avg_leg_vol": round(avg_vol, 1),
        "diversification_pp": round(avg_vol - ew_vol, 1),
        "diversification_pct": round((avg_vol - ew_vol) / avg_vol * 100, 1) if avg_vol else None,
        "vol_ratio": round(max(vols.values()) / min(vols.values()), 2) if min(vols.values()) else None,
    }


def _bands_outline(bands: list[dict], totals: dict, driver_id: str) -> list[dict]:
    """全部档位的全景（含当前命中档与「距下一档还差几分」）。

    存在理由：只显示当前档，读者不知道「再走多远会换档」——而权重是跟着档位
    跳变的，阈值附近的边际变化恰恰是决策最关心的部分。
    """
    cur_total = totals.get(driver_id)
    out, hit = [], False
    for b in bands:
        is_cur = False
        if not hit and _band_matches(b, totals, cur_total):
            is_cur = hit = True
        out.append({
            "label": b.get("label"), "min_total": b.get("min_total"),
            "weights": b.get("weights"), "note": b.get("note"),
            "current": is_cur,
        })
    # 「距上一档（更好的档）还差多少分」——bands 降序，当前档的前一条即更优档
    gap = None
    idx = next((i for i, x in enumerate(out) if x["current"]), None)
    if idx is not None and idx > 0 and cur_total is not None:
        need = out[idx - 1].get("min_total")
        if need is not None:
            gap = {"to_label": out[idx - 1]["label"], "need_total": need,
                   "delta": round(need - cur_total, 2)}
    return {"bands": out, "current_index": idx, "upgrade_gap": gap,
            "driver": driver_id, "driver_total": cur_total}


def list_portfolios(storage=None) -> list[dict]:
    """全部组合的当前建议（多组合视图用）。单个组合算失败不影响其余。"""
    storage = storage or _default_storage()
    out: list[dict] = []
    for pf in load_portfolios():
        try:
            out.append(get_portfolio_suggestion(storage=storage, portfolio_id=pf.get("id")))
        except Exception as e:        # 单个组合坏定义不该让整个视图白屏
            logger.warning("[Thesis] 组合 %s 建议计算失败: %s", pf.get("id"), e)
            out.append({"ok": False, "id": pf.get("id"), "name": pf.get("name"),
                        "reason": str(e)[:120]})
    return out


def _band_matches(band: dict, totals: dict, driver_total=None) -> bool:
    """定档命中判定。

    - v2：`conditions` 为多条件列表（**AND 语义**，全部满足才命中）
    - v1：`min_total` 对**声明的驱动腿**比较——向后兼容既有 5 档写法

    ⚠️ v1 分支必须显式用 driver_total，**不能**「取第一个有快照的腿」：
    legs 的书写顺序是防守腿在前（dividend_env → hang_seng_tech），
    按顺序取会拿到防守腿的分去对进攻腿的档位，选档静默错位。

    任一条件依赖的腿没有快照（total=None）时判不命中：宁可不定档，也不拿半截数据下结论。
    """
    conds = band.get("conditions")
    if isinstance(conds, list) and conds:
        return all(_cond_holds(c, totals) for c in conds)
    if "min_total" in band and driver_total is not None:
        return driver_total >= band.get("min_total", -999)
    return False


def _cond_holds(cond: dict, totals: dict) -> bool:
    """单条件判定：支持 min_total/max_total 与通用 op/value 两种写法。"""
    if not isinstance(cond, dict):
        return False
    total = totals.get(cond.get("thesis_id"))
    if total is None:
        return False
    if "min_total" in cond:
        return total >= cond["min_total"]
    if "max_total" in cond:
        return total <= cond["max_total"]
    op, val = cond.get("op"), cond.get("value")
    if op is None or val is None:
        return False
    try:
        if op == "<":
            return total < val
        if op == "<=":
            return total <= val
        if op == ">":
            return total > val
        if op == ">=":
            return total >= val
        if op == "==":
            return total == val
    except TypeError:
        return False
    return False


def _eval_co_exposure(pf: dict, totals: dict) -> list[dict]:
    """评估组合的**共同风险因子**（多腿共享的底层暴露）。

    存在理由：当前体系两腿同弱时只会「内部调权重」，但若两腿共享同一底层暴露
    （如恒科与红利都吃中国内需），内部调权只是把风险从一条腿搬到另一条腿，
    正确动作是「整体降仓」。这条判断在体系里原本无处承载。

    任一条件依赖的腿缺快照 → 该风险**不触发**（保守：不拿半截数据报风险），
    但在返回里标 `evaluable: False`，让前端能区分「没触发」与「评不了」。
    """
    out: list[dict] = []
    for risk in ((pf.get("co_exposure") or {}).get("shared_risks") or []):
        trig = risk.get("trigger") or []
        evaluable = all(totals.get(c.get("thesis_id")) is not None for c in trig)
        hit = evaluable and all(_cond_holds(c, totals) for c in trig)
        out.append({
            "factor": risk.get("factor"), "label": risk.get("label"),
            "legs": risk.get("legs"), "rationale": risk.get("rationale"),
            "trigger": trig, "action": risk.get("action"),
            "confidence": risk.get("confidence"),
            "triggered": hit, "evaluable": evaluable,
        })
    return out


# ── 市场矩阵：资产 × 窗口热力图 + 相关性矩阵 ──────────────────────────

# 覆盖三个台账的全部标的 + 共同基准（都在 index_daily 里）
_MATRIX_UNIVERSE = [
    ("H30269", "红利低波"),
    ("HSTECH", "恒生科技"),
    ("HSI", "恒生指数"),
    ("000300", "沪深300"),
    ("H11001", "中证全债"),
]

_MATRIX_WINDOWS = [("1日", 1), ("5日", 5), ("20日", 20), ("60日", 60), ("年内", None)]


def _pct_change(closes: list[float], n: int | None, dates: list[str]) -> float | None:
    """区间涨跌幅（%）。n=None 表示「年内」——从当年第一个交易日算起。"""
    if not closes:
        return None
    if n is None:
        yr = str(dates[-1])[:4]
        base_idx = next((i for i, d in enumerate(dates) if str(d)[:4] == yr), None)
        if base_idx is None or base_idx >= len(closes) - 1:
            return None
        base = closes[base_idx]
    else:
        if len(closes) < n + 1:
            return None
        base = closes[-(n + 1)]
    if not base:
        return None
    return round((closes[-1] / base - 1) * 100, 2)


def get_market_matrix(days: int = 260) -> dict:
    """资产 × 时间窗口涨跌幅热力图 + 相关性矩阵（全部本地日线自算）。

    存在理由：涨跌幅在短/长窗口上的分布差异，用热力图一眼就能说清；台账原先只有
    单资产读数，读者无法横向对比，也难以复核「两腿波动结构」这类写死在文案里的常量。
    取不到的标的直接跳过并在 `missing` 里点名，不猜值。
    """
    import numpy as np
    from utils.cache.market_data_db import get_index_daily

    rows, series, missing = [], {}, []
    common: list = []          # 各标的的共同交易日（在下方相关性块里填充）
    for code, name in _MATRIX_UNIVERSE:
        try:
            df = get_index_daily(code)
        except Exception as e:
            logger.warning("[Thesis] 矩阵取 %s 日线失败: %s", code, e)
            df = None
        if df is None or len(df) < 21:
            missing.append({"code": code, "name": name})
            continue
        df = df.tail(days)
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]
        closes = [float(x) for x in df["close"].tolist()]
        rows.append({
            "code": code, "name": name, "last": round(closes[-1], 2),
            "values": [_pct_change(closes, n, dates) for _, n in _MATRIX_WINDOWS],
        })
        series[name] = (closes, dates)

    # 相关性：各标的日收益的 Pearson（共同交易日对齐）
    labels = [r["name"] for r in rows]
    matrix = []
    if len(labels) >= 2:
        # 先按日期取交集，避免不同市场休市日错位造成伪相关
        common = None
        for name in labels:
            d = set(series[name][1])
            common = d if common is None else (common & d)
        common = sorted(common or [])
        if len(common) > 30:
            vectors = []
            for name in labels:
                m = dict(zip(series[name][1], series[name][0]))
                vectors.append([m[d] for d in common])
            arr = np.array(vectors)
            rets = np.diff(arr, axis=1) / arr[:, :-1]      # 日收益
            rets = np.where(np.isfinite(rets), rets, 0.0)
            with np.errstate(invalid="ignore", divide="ignore"):
                matrix = np.corrcoef(rets).round(2).tolist()

    return {
        "asof": str(series[labels[0]][1][-1])[:10] if labels else "",
        "windows": [w for w, _ in _MATRIX_WINDOWS],
        "rows": rows,
        "correlation": {
            "labels": labels,
            "matrix": matrix,
            "basis": "近 %d 个共同交易日的日收益（Pearson）" % (len(common) if common else 0),
        },
        "missing": missing,
    }


# ── 因子证据：因子历史序列 vs 标的未来收益 ────────────────────────────

_EVIDENCE_TARGET = {
    "dividend_env": ("H30269", "红利低波"),
    "hang_seng_tech": ("HSTECH", "恒生科技"),
    "bond_fund": ("H11001", "中证全债"),
    "hk_dividend": ("930914", "港股通高股息"),
}

# 复合/手工因子的历史序列不再由本模块各写一套构造（那会与引擎口径漂移）：
# 形态与历史重放统一走 `thesis.evidence`——对 ctx 做截断后调 FACTOR_COMPUTERS。

# 因子证据结果缓存：{(thesis_id, factor_key): {"day": ISO日期, "data": 结果}}。
# evidence 首次计算会触发引擎补数（实测 ~25s），同一天内重复点开必须走缓存；
# 点「刷新」时由 refresh() → invalidate_evidence_cache() 主动清掉，保证刷新后重算。
_EVIDENCE_CACHE: dict = {}


def invalidate_evidence_cache(thesis_id: str | None = None) -> int:
    """清掉因子证据缓存。`thesis_id` 为空时清全部。返回清除的条数。"""
    if thesis_id is None:
        n = len(_EVIDENCE_CACHE)
        _EVIDENCE_CACHE.clear()
        return n
    keys = [k for k in _EVIDENCE_CACHE if k[0] == thesis_id]
    for k in keys:
        _EVIDENCE_CACHE.pop(k, None)
    return len(keys)


# 报告数据缓存：{(thesis_id, db_path): {"at": monotonic, "data": 报告}}。
# 报告组装会触发 engine._prepare（实测 5~70s），用户同一张台账反复点「生成报告」
# 不应该每次重算；30 分钟 TTL + 点「刷新」主动失效，够覆盖「看一眼、调下格式、
# 再导出」的连续操作。前端还按 (thesis_id) 缓存 PNG blob（30 分钟），两层缓存
# 分别省掉「后端重算」与「浏览器重画 + 重编码」。
REPORT_CACHE_TTL_S = 30 * 60
_REPORT_CACHE: dict = {}


def invalidate_report_cache(thesis_id: str | None = None) -> int:
    """清掉报告数据缓存。`thesis_id` 为空时清全部。返回清除的条数。"""
    if thesis_id is None:
        n = len(_REPORT_CACHE)
        _REPORT_CACHE.clear()
        return n
    keys = [k for k in _REPORT_CACHE if k[0] == thesis_id]
    for k in keys:
        _REPORT_CACHE.pop(k, None)
    return len(keys)


def get_factor_evidence(thesis_id: str, factor_key: str) -> dict:
    """单因子的关联性证据（带按天缓存）。计算主体见 `_compute_factor_evidence`。

    缓存键 = (thesis_id, factor_key)，**按天失效**；点「刷新」时由 refresh()
    主动清掉，保证刷新后重算。evidence 首次计算会触发引擎补数（实测 ~25s），
    缓存就是为了把这个成本收敛到每天一次。
    """
    from datetime import date

    day = date.today().isoformat()
    ckey = (thesis_id, factor_key)
    cached = _EVIDENCE_CACHE.get(ckey)
    if cached and cached["day"] == day:
        return cached["data"]
    result = _compute_factor_evidence(thesis_id, factor_key)
    _EVIDENCE_CACHE[ckey] = {"day": day, "data": result}
    return result


def _chart_offline_engine(engine):
    """把引擎切成「只读本地库」：不补数、不抓指数估值。

    用途：`refresh_background_data.py` 在 `thesis_refresh` 作业**之后**预热双轴图，
    数据刚拉过，再联网既慢（实测每张 26~65s，多为超时）又白耗上游配额。补丁只
    作用于传入的引擎实例，不影响 web 请求里正常的取数路径。

    ⚠️ 只适合「刚刷新过」的预热：本地库缺数据时因子会被判 dropped，不会去补。
    """
    def _no_refresh(*a, **k):
        return False

    def _no_collect(*a, **k):
        raise RuntimeError("offline 模式不采集日线")

    def _no_fetch(*a, **k):
        raise RuntimeError("offline 模式不抓指数估值")

    def _sync_codes(code):
        c = str(code).strip().upper()
        return {"dy_code": f"idx_{c.lower()}_dy",
                "pe_code": f"idx_{c.lower()}_pe",
                "dy_added": 0, "ok": False}

    engine._needs_refresh = _no_refresh
    engine._index_needs_refresh = _no_refresh
    engine.collect_index_daily = _no_collect
    engine.fetch_index_valuation = _no_fetch
    engine.sync_index_valuation = _sync_codes
    return engine


def _evidence_prep(thesis_id: str, offline: bool = False) -> dict:
    """证据计算的**共享准备**：定义 + 引擎 + ctx + 目标标的 + 快照历史。

    单独抽出来的理由：`engine._prepare()` 会触发取数补数（实测 ~25s）。
    单因子点开时跑一次无所谓；批量校验 / 报告组装时逐因子重跑会线性累加，
    故由调用方准备一次、全因子复用。

    `offline=True` 时把引擎切成只读本地库（`_chart_offline_engine`），用于
    refresh 作业之后的图表预热——数据刚拉过，不再重复联网。

    `target` 由 `thesis.evidence.resolve_target()` 解析：定义里的
    `evidence.target`（权威）→ `_EVIDENCE_TARGET`（老台账回退）→
    subject.index_code（最后兜底）。`history` 供手工因子用快照累积值取证。
    """
    from thesis import defs as thesis_defs
    from thesis import evidence as thesis_evidence

    defn, _ = thesis_defs.load_def_checked(thesis_id)
    if defn is None:
        return {"error": "台账定义不可用"}
    engine = _default_engine()
    if offline:
        engine = _chart_offline_engine(engine)
    params = defn.get("params") or {}
    ctx = engine._prepare(defn, params, [])
    target = thesis_evidence.resolve_target(
        defn, ctx, fallback=_EVIDENCE_TARGET.get(thesis_id))
    history: list[dict] = []
    try:
        history = _default_storage().get_history(thesis_id, days=400)
    except Exception as e:            # 快照读不到只影响手工因子，不该整块失败
        logger.warning("[Thesis] 证据快照历史读取失败(%s): %s", thesis_id, e)
    return {"defn": defn, "engine": engine, "params": params, "ctx": ctx,
            "target": target, "history": history,
            "tgt_code": (target or {}).get("code") or "",
            "tgt_name": (target or {}).get("name") or ""}


def _prep_target(prep: dict):
    """取 prep 里解析好的 target；兼容旧 prep 形态（tgt_code + df 现场构造）。"""
    from thesis import evidence as thesis_evidence

    target = prep.get("target")
    if target:
        return target
    code = prep.get("tgt_code") or ""
    if not code:
        return None
    return thesis_evidence.target_from_frame(
        prep.get("df"), code=code, name=prep.get("tgt_name") or "",
        alias="", unit="")


def _compute_factor_evidence(thesis_id: str, factor_key: str,
                              prep: dict | None = None) -> dict:
    """单因子的关联性证据（契约 5.2）：历史重放 / 快照 vs 标的未来收益。

    统计与口径全部在 `thesis.evidence`（纯函数，可离线单测）；这里只负责
    找因子定义、取 prep 里的 ctx/target/快照，交给 `build_evidence`。
    **不做假图**：无目标标的、无序列、样本不足一律 supported=False 并说明原因。

    `prep` 为 `_evidence_prep()` 的结果；批量调用时传入以复用 ctx（省掉逐因子补数）。
    """
    from thesis import evidence as thesis_evidence

    prep = prep or _evidence_prep(thesis_id)
    if prep.get("error"):
        return {"supported": False, "reason": prep["error"]}

    defn = prep.get("defn") or {}
    spec = next((f for f in defn.get("factors") or []
                 if f.get("key") == factor_key), None)
    if spec is None:
        return {"supported": False, "reason": "因子不存在: %s" % factor_key}

    return thesis_evidence.build_evidence(
        defn, spec,
        ctx=prep.get("ctx") or {},
        engine=prep.get("engine"),
        params=prep.get("params") or {},
        target=_prep_target(prep),
        snapshot_rows=thesis_evidence.snapshot_series(
            prep.get("history") or [], factor_key),
    )


def _best_horizon(ev: dict) -> dict | None:
    """取相关系数绝对值最大的期限作代表。

    **优先常规期限**：`short_window=True` 的短窗口（如未来 5 个交易日）只有在
    常规期限样本都不足时才会由 `thesis.evidence` 追加，它的 r 天生更容易偏大
    （窗口越短、重叠越严重、噪声越多）。一旦有常规期限可用，就不该让短窗口
    决定「这个因子强不强」的档位。
    """
    hs = [h for h in (ev.get("horizons") or [])
          if h.get("corr") is not None and not h.get("short_window")]
    if not hs:
        hs = [h for h in (ev.get("horizons") or []) if h.get("corr") is not None]
    return max(hs, key=lambda h: abs(h["corr"])) if hs else None


def _evidence_tier(ev: dict) -> str:
    """把因子证据分成五档，供汇总与准入检查。

    阈值与 `_evidence_verdict` 的人话结论**必须一致**，否则会出现
    「汇总说强、点开说弱」的自相矛盾。

    `insufficient` 与 `unsupported` 必须分开，否则会误导：
    - **insufficient**：序列太短（多为指数估值类因子——源只给近 20 个交易日，
      靠逐期累积，见 `sync_index_valuation`）。**样本在长，等得起**。
    - **unsupported**：复合/手工因子（双轴矩阵、相对强弱、手工项），
      结构上就没有单一时间序列可回放，**永远验不了**。
    """
    if not ev.get("supported"):
        return "insufficient" if "样本不足" in (ev.get("reason") or "") else "unsupported"
    best = _best_horizon(ev)
    if best is None:
        return "insufficient"
    if best.get("short_window"):
        # 短窗口只在常规期限样本不足时兜底：r 天生更飘（收益重叠更严重），
        # 最多算「弱支撑」，否则会在汇总里把「刚积累一个月的因子」评成强。
        return "weak"
    c, q = abs(best["corr"]), abs(best.get("q5_q1") or 0)
    if c >= 0.3 and q >= 2:
        return "strong"
    if c >= 0.15:
        return "medium"
    return "weak"


def get_all_factor_evidence(thesis_id: str, details: bool = False) -> dict:
    """一张台账**全部因子**的有效性汇总（因子有效性回测的产品化入口）。

    存在理由：单因子证据要「点开某个因子」才看得到，回答不了「这张台账的因子
    有几个真有区分度、有几个是凑数的」。新增台账 / 新增因子的**准入检查**需要的
    正是这个横向视图——否则每建一张台账都得逐因子手工点一遍。

    与 `get_factor_evidence` 的差别：ctx 只准备一次（见 `_evidence_prep`），
    因此批量明显快于逐因子串行；同时**不逐因子按天缓存**（汇总口径不同）。

    `details=True` 额外带滚动相关曲线与分层明细（报告用，体积明显更大）。
    """
    prep = _evidence_prep(thesis_id)
    if prep.get("error"):
        return {"thesis_id": thesis_id, "ok": False, "reason": prep["error"], "rows": []}
    defn = prep["defn"]
    target = prep.get("target") or {}
    rows = []
    tiers = {"strong": 0, "medium": 0, "weak": 0, "insufficient": 0, "unsupported": 0}
    for spec in defn.get("factors") or []:
        key = spec.get("key")
        try:
            ev = _compute_factor_evidence(thesis_id, key, prep=prep)
        except Exception as e:        # 单因子坏掉不能毁掉整张表
            logger.warning("[Thesis] 因子 %s 证据计算失败: %s", key, e)
            ev = {"supported": False, "reason": "证据计算失败：%s" % str(e)[:120]}
        tier = _evidence_tier(ev)
        tiers[tier] += 1
        best = _best_horizon(ev) or {}
        cur = ev.get("current") or {}
        rolling = ev.get("rolling") or {}
        direction = ev.get("direction") or {}
        primary = (ev.get("horizons") or [None])[0] or {}
        # corr 可能全为 null（各期限样本都 <30）：此时 h/n 回落到首个期限，
        # 报告表仍能显示「未来 20 日 / n=24 / 相关系数 —」，而不是整列空白。
        row_n = best.get("n")
        if row_n is None:
            row_n = primary.get("n")
        row = {
            "factor_key": key,
            "factor_name": ev.get("factor_name") or spec.get("name") or key,
            "tier": tier,
            "supported": bool(ev.get("supported")),
            "reason": ev.get("reason"),
            "h": best.get("h", primary.get("h")), "corr": best.get("corr"),
            "corr_score": best.get("corr_score"),
            "q5_q1": best.get("q5_q1"), "n": row_n,
            "verdict": ev.get("verdict") or ev.get("reason"),
            "confidence": (spec.get("confidence") or {}).get("level"),
            # ↓ 契约 5.3 新增：当前读数位置 / 滚动相关现状 / 方向校验
            "unit": ev.get("unit") or spec.get("unit") or "",
            "discrete": bool(ev.get("discrete")),
            "source": ev.get("source"),
            "current_value": cur.get("value"),
            "percentile": cur.get("percentile"),
            "current_bucket": cur.get("bucket_code"),
            "bucket_mean_return": cur.get("bucket_mean_return"),
            "bucket_win_rate": cur.get("bucket_win_rate"),
            # 胜率是「当前桶」的胜率，分母必须是该桶的样本数；报告表若拿全样本
            # n 去除它，会出现「20% / 46」这种把 10 个桶样本说成 46 个的误导。
            "bucket_n": cur.get("bucket_n"),
            "rolling_current": rolling.get("current_corr_value"),
            "rolling_positive_share": rolling.get("positive_share"),
            "direction": direction.get("expected_sign"),
            "aligned": direction.get("aligned"),
            # 方向校验固定跑「首个可用期限」（与滚动/散点/当前桶同口径），
            # 与上面的 h（|r| 最大的代表期限）可能不同，故显式带出来。
            "direction_h": direction.get("h"),
            # 代表期限是否为「短窗口兜底」（常规 20/60 日样本不足时才会出现）
            "short_window": bool(best.get("short_window")),
        }
        if details:                   # 报告用：滚动曲线 + 分层明细
            row["rolling"] = {"dates": rolling.get("dates") or [],
                              "corr_value": rolling.get("corr_value") or []}
            row["layers"] = primary.get("layers") or []
        rows.append(row)
    return {"thesis_id": thesis_id, "ok": True,
            "name": defn.get("name", thesis_id),
            "target": {"code": target.get("code", ""),
                       "name": target.get("name", ""),
                       "kind": target.get("kind"),
                       "freq": target.get("freq"),
                       "freq_label": target.get("freq_label")},
            "summary": tiers,
            "usable": tiers["strong"] + tiers["medium"],
            "verifiable": len(rows) - tiers["unsupported"],
            "factor_count": len(rows), "rows": rows}


# ── 双轴复盘图：总分 vs 标的价格（三级缓存：L1 进程 1h / L2 持久 1h / L3 因子 24h） ──

# 目标频率 → 未来验证期数。与 `thesis.evidence` 首档 horizons 同口径
# （日 20 个交易日 / 周 4 周 / 月 6 个月），改这里等于改「验证期」的定义。
CHART_HORIZON_BY_FREQ = {"daily": 20, "weekly": 4, "monthly": 6}

# 覆盖率面板最少期数：低于这个数不给图（样本还在长，等得起；给了会被当真）
CHART_MIN_PERIODS = 100

# 前端默认返回点数上限（超出按等步长抽稀；抽稀只影响画图，不影响指标口径）
CHART_DEFAULT_POINTS = 1200
CHART_MAX_POINTS = 5000

# L1 进程内缓存 TTL：1 小时。同一份定义、1 小时内重复打开同一张图必须直接复用
# （台账总览一次要 8 张，每张冷算 5~30s 是不可接受的）。
CHART_CACHE_TTL_S = 60 * 60

# L2 持久缓存 TTL（cache_kv，跨进程：预热脚本写、web server 读）。与 L1 同为
# 1 小时：这期间重复打开命中；之后由 L3 只重放「输入轴前进」的因子，不整图重算。
CHART_CACHE_L2_EXPIRE_HOURS = 1

# L3 因子级缓存 TTL：24 小时。真正的失效靠 key 里的 input_end（因子输入轴最后
# 日期）：轴没前进就复用 rows（不 replay）；24h 只是兜底——数据被修订但没有新
# 日期时，最多旧一天。
CHART_FACTOR_CACHE_TTL_S = 24 * 60 * 60
CHART_FACTOR_CACHE_L2_EXPIRE_HOURS = 24

# L1 双轴图结果：{(thesis_id, 定义哈希, h): {"record", "data_end", "computed_at", "at"}}。
# **key 必须含定义内容哈希**——台账是定义驱动的，阈值/因子/权重一改就必须重算；
# 只按 thesis_id 缓存会把「改完阈值页面还显示旧图」留成静默错误。
# **key 刻意不含 data_end**：用户要的是「研究版本没变，1 小时内别再算」——即使
# 目标序列前进一天也直接复用；TTL 到期后再探测数据日，没变就只续计时、不重算。
_CHART_CACHE: dict = {}

# 「上次算出的数据日 / 验证期」提示：{(thesis_id, 定义哈希): {"end", "h"}}。
# 用途：① 探测失效（如 `kind=index_valuation` 的目标别名要 sync 才知道派生码，
# 探测会联网）时仍有 h 可拼缓存 key；② L2 命中后回填，进程重启不必每次等探测。
_CHART_HINT: dict = {}

# L3 因子重放结果（进程内镜像 L2）：{fac_key: {"rows", "input_end", "at"}}。
# key 见 chart_factor_cache_key()；rows 是 JSON 安全化后的 [{date,value,score}]。
_CHART_FACTOR_CACHE: dict = {}

# 最近一次 `_chart_compute` 的 L3 命中统计（观测用，不进 API 契约）。
_LAST_FACTOR_STATS: dict = {}


def chart_horizon(freq: str | None) -> int:
    """目标频率 → 未来验证期数（日 20 / 周 4 / 月 6）。"""
    return CHART_HORIZON_BY_FREQ.get(freq or "daily", 20)


def chart_def_hash(defn: dict) -> str:
    """台账定义内容哈希（16 位，sort_keys 与键序无关）。

    直接用快照存储的 `content_hash`：同一份定义在「定义留痕」和「图表缓存」
    两处口径一致，不会出现「怀疑定义变了但哈希没变」的第二种事实源。
    """
    from thesis.storage import content_hash
    return content_hash(defn or {})


def chart_cache_key(thesis_id: str, def_hash: str, h: int) -> tuple:
    """L1/L2 双轴图缓存 key = (thesis_id, 定义内容哈希, 验证期数)。

    刻意**不含目标序列最后日期**：用户在意的口径是「同一份定义、1 小时内」，
    数据日只用于 TTL 到期后的「要不要重算」判断（见 get_chart_data）。
    定义一改（阈值/因子/权重）→ def_hash 变 → key 变，绝不会返回旧图。
    """
    return (thesis_id, str(def_hash or ""), int(h))


def chart_cache_l2_key(thesis_id: str, def_hash: str, h: int) -> str:
    """L2 持久缓存 key = `{thesis_id}:{def_hash}:{h}`（cache_kv 里再拼表前缀）。"""
    return "%s:%s:%d" % (thesis_id, def_hash or "", int(h))


def chart_factor_cache_key(thesis_id: str, def_hash: str, factor_key: str) -> str:
    """L3 因子缓存 key = `{thesis_id}:{def_hash}:{factor_key}`。"""
    return "%s:%s:%s" % (thesis_id, def_hash or "", factor_key)


def invalidate_chart_cache(thesis_id: str | None = None) -> int:
    """清掉双轴图缓存：L1（本进程）+ 提示 + L2（持久，按前缀删）。

    返回清掉的 L1 条数（调用方关心的是「这张图还在不在」）。
    **L3 因子缓存刻意不清**：新定义已有新 def_hash、新数据已有新 input_end，
    保留它正是「有新数据只算新增/必要部分」的实现。`thesis_id` 为空时清全部。

    ⚠️ 清 L2 走 `del_thesis_chart_cache_prefix` 的 GLOB（不是 LIKE）：thesis_id
    里全是下划线，LIKE 会把 `bondXfund` 误删——见 utils/cache/core.py。
    """
    if thesis_id is None:
        n = len(_CHART_CACHE)
        _CHART_CACHE.clear()
        _CHART_HINT.clear()
        _CHART_FACTOR_CACHE.clear()
        _del_chart_l2("")
        return n
    keys = [k for k in _CHART_CACHE if k and k[0] == thesis_id]
    for k in keys:
        _CHART_CACHE.pop(k, None)
    for k in [k for k in _CHART_HINT if k and k[0] == thesis_id]:
        _CHART_HINT.pop(k, None)
    _del_chart_l2(thesis_id + ":")
    return len(keys)


def _chart_cache_prune(now: float | None = None) -> int:
    """清掉过期 L1（不同定义版本 / h / 台账会各存一份，不清理会缓慢膨胀）。"""
    now = time.monotonic() if now is None else now
    keys = [k for k, v in list(_CHART_CACHE.items())
            if (now - float(v.get("at") or 0.0)) > CHART_CACHE_TTL_S]
    for k in keys:
        _CHART_CACHE.pop(k, None)
    return len(keys)


def _chart_factor_prune(now: float | None = None) -> int:
    """清掉过期 L3 因子缓存（24h 兜底；正常失效靠 input_end 前进）。"""
    now = time.monotonic() if now is None else now
    keys = [k for k, v in list(_CHART_FACTOR_CACHE.items())
            if (now - float(v.get("at") or 0.0)) > CHART_FACTOR_CACHE_TTL_S]
    for k in keys:
        _CHART_FACTOR_CACHE.pop(k, None)
    return len(keys)


# ── 三级缓存：L2 持久层读写（L1 是上面的 dict，L3 在 _chart_compute 里） ──

def _del_chart_l2(prefix: str) -> int:
    """删 L2 双轴图前缀（best-effort：缓存库不可用不能影响出图）。"""
    try:
        from utils.cache import api as cache_api
        return cache_api.del_thesis_chart_cache_prefix(prefix)
    except Exception as e:              # noqa: BLE001 - 缓存故障只降级
        logger.debug("[Thesis] 双轴图 L2 前缀清理失败(%s): %s", prefix, e)
        return 0


def _cache_age_s(computed_at) -> float | None:
    """`computed_at`（本地时间字符串）→ 距今秒数；解析失败返回 None。"""
    from datetime import datetime

    if not computed_at:
        return None
    try:
        dt = datetime.strptime(str(computed_at), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0.0, round((datetime.now() - dt).total_seconds(), 1))


def _read_chart_l2(thesis_id: str, def_hash: str, h: int) -> dict | None:
    """读 L2 双轴图（跨进程：预热脚本写、web server 复用）。坏数据视为未命中。"""
    try:
        from utils.cache import api as cache_api
        data = cache_api.get_thesis_chart_cache(
            chart_cache_l2_key(thesis_id, def_hash, h))
    except Exception as e:              # noqa: BLE001 - 缓存故障只降级
        logger.debug("[Thesis] 双轴图 L2 读取失败(%s): %s", thesis_id, e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("record"), dict):
        return None
    return data


def _write_chart_l2(thesis_id: str, def_hash: str, h: int, entry: dict) -> None:
    """写 L2 双轴图（只存 record + data_end + computed_at，TTL 1h）。"""
    try:
        from utils.cache import api as cache_api
        cache_api.set_thesis_chart_cache(
            chart_cache_l2_key(thesis_id, def_hash, h),
            {"record": entry.get("record") or {},
             "data_end": entry.get("data_end"),
             "computed_at": entry.get("computed_at")},
            expire_hours=CHART_CACHE_L2_EXPIRE_HOURS)
    except Exception as e:              # noqa: BLE001 - 缓存故障只降级
        logger.debug("[Thesis] 双轴图 L2 写入失败(%s): %s", thesis_id, e)


def _cached_chart_payload(entry: dict, points: int) -> dict:
    """L1/L2 命中 → 接口形状（cached=True + computed_at / cache_age_s）。"""
    return chart_payload(entry.get("record") or {}, points, cached=True,
                         computed_at=entry.get("computed_at"),
                         cache_age_s=_cache_age_s(entry.get("computed_at")))


def _chart_l2_payload(thesis_id: str, def_hash: str, h: int, points: int) -> dict | None:
    """查 L2：命中则回填 L1 + 提示并返回接口形状，未命中返回 None。

    回填 L1 的 TTL 从「真正算完」时刻起算（`at = now - age`），不能因为换了一层
    缓存就把 1 小时续成 2 小时。
    """
    l2 = _read_chart_l2(thesis_id, def_hash, h)
    if not l2:
        return None
    now = time.monotonic()
    entry = {"record": l2.get("record") or {},
             "data_end": l2.get("data_end"),
             "computed_at": l2.get("computed_at"),
             "at": now}
    age = _cache_age_s(entry["computed_at"])
    if age is not None:
        entry["at"] = now - min(float(age), float(CHART_CACHE_TTL_S))
    _CHART_CACHE[chart_cache_key(thesis_id, def_hash, h)] = entry
    _CHART_HINT[(thesis_id, def_hash)] = {"end": entry["data_end"], "h": int(h)}
    logger.debug("[Thesis] 双轴图 L2 命中 %s def=%s h=%d age=%ss",
                 thesis_id, str(def_hash)[:8], int(h), age)
    return _cached_chart_payload(entry, points)


# ── 双轴图：可单测的纯逻辑（抽稀 / 指标 / 未来价格 / 覆盖率面板） ────────

def thin_indices(n: int, max_points: int) -> tuple[list[int], int]:
    """等步长抽稀索引：不超过 max_points，且**必含最后一点**。

    与 `thesis.evidence._sample_axis` 同一策略（等步长 + 含最后一天 + 封顶）：
    图里少了最后一点，用户会以为「数据停在几天前」。返回 (索引列表, 步长)，
    步长 1 表示未抽稀。
    """
    n = int(n or 0)
    if n <= 0:
        return [], 1
    max_points = max(2, int(max_points or CHART_DEFAULT_POINTS))
    if n <= max_points:
        return list(range(n)), 1
    step = max(1, math.ceil(n / max_points))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    if len(idx) > max_points:              # 补尾点后可能比上限多 1 个
        idx = idx[:max_points - 1] + [n - 1]
    return idx, step


def fut_price_series(price, h: int) -> list:
    """fut[i] = price[i + h]（未来 h 期价格），尾部 h 个为 None。

    图上拿「今天的高分 → h 天后价格是否更高」做验证；尾部没有未来数据就必须
    留空，不能用 0 或重复最后价补——那会画出一条假的水平延长线。
    """
    # 入参可能是 list 也可能是 numpy 数组：先 list() 再转 float（不能写 `price or []`，
    # 数组的真值判断会抛「ambiguous」）
    seq = list(price) if price is not None else []
    vals = [float(x) for x in seq]
    n = len(vals)
    h = max(0, int(h or 0))
    out: list = [None] * n
    for i in range(max(0, n - h)):
        out[i] = vals[i + h]
    return out


def select_chart_panel(first_indexes, n_dates: int,
                       min_periods: int = CHART_MIN_PERIODS):
    """覆盖率面板：从 K 个因子往下选，保证窗口至少 min_periods 期。

    每个因子有自己的「首个有效值位置」（宏观因子起步晚、指数估值只有近几十个点）。
    想保留全部因子就得等最晚起步的那个；窗口不够长就丢掉最晚起步的因子
    （调用方记入 dropped），直到窗口够长。

    返回 (保留因子在入参中的位置列表, 面板起点)；连最早起步的因子都撑不满时返回 None。
    """
    n = int(n_dates or 0)
    firsts = [int(x) for x in (first_indexes or [])]
    if n <= 0 or not firsts or int(min_periods or 0) <= 0:
        return None
    order = sorted(firsts)
    for need in range(len(order), 0, -1):
        cand = order[need - 1]
        if n - cand >= int(min_periods):
            return [i for i, f in enumerate(firsts) if f <= cand], cand
    return None


def flip_score_series(panel, fwd) -> tuple[list[float], list[int]]:
    """按每个因子相对未来收益的 Spearman 符号翻正后求和（内样本参考线）。

    符号 +1/-1：因子与未来 h 期收益正相关就按原分累加，负相关就翻正。
    样本不足（r 为 None）或 r >= 0 一律取 +1——**没有证据就不翻**，
    否则等于拿噪声造一条「看起来更贴价格」的线。
    返回 (flip 列表, 符号列表)，符号与因子列同序。
    """
    import numpy as np

    from thesis.evidence import spearman

    S = np.asarray(panel, dtype=float)
    y = np.asarray(fwd, dtype=float)
    if S.ndim != 2 or S.shape[1] == 0:
        return [], []
    signs: list[int] = []
    for j in range(S.shape[1]):
        r = spearman(S[:, j], y)
        signs.append(1 if (r is None or r >= 0) else -1)
    flip = (S * np.asarray(signs, dtype=float)).sum(axis=1)
    return [round(float(x), 4) for x in flip], signs


def chart_metrics(total, price, fwd, flip) -> dict:
    """双轴图四个相关性指标（全部 Spearman，nan 自动剔除）。

    - r_level   ：总分 vs 价格水平
    - r_diff    ：总分变动 vs 价格变动
    - r_fwd     ：总分 vs 未来 h 期收益（真正要看的是这个）
    - r_fwd_flip：翻转后总分 vs 未来 h 期收益

    **必须用抽稀前全量算**：抽稀只是画图省点，统计口径不能被它改。
    """
    import numpy as np

    from thesis.evidence import spearman

    t = np.asarray(total, dtype=float)
    p = np.asarray(price, dtype=float)
    f = np.asarray(fwd, dtype=float)
    fl = np.asarray(flip, dtype=float)
    return {
        "r_level": _chart_round(spearman(t, p)),
        "r_diff": _chart_round(spearman(np.diff(t), np.diff(p))),
        "r_fwd": _chart_round(spearman(t, f)),
        "r_fwd_flip": _chart_round(spearman(fl, f)),
    }


def _chart_round(v):
    """相关系数保留 4 位（JSON 安全：None/NaN 原样返回 None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if math.isfinite(f) else None


def chart_payload(record: dict, points: int = CHART_DEFAULT_POINTS,
                  cached: bool = False, computed_at: str | None = None,
                  cache_age_s: float | None = None) -> dict:
    """把「全量记录」裁成接口形状：抽稀四个数组 + 记录 points/thin/cached。

    **指标不重算**：metrics 来自抽稀前的全量序列；抽稀只影响画图分辨率。
    `n` 是抽稀前的有效期数（统计口径），`points` 才是实际返回点数——
    两个数都在响应里，前端展示「样本 N 期」用 n，画图用数组长度。

    缓存观测字段：`computed_at` 是**真正算完**的时刻（复用/续期都不改它），
    `cache_age_s` = 距真正算完的秒数——所以 TTL 到期但数据日没变被复用时，
    会看到 `cached=True` 而 `cache_age_s≈3600`，这是诚实口径（图是 1 小时前算的）。
    """
    record = dict(record or {})
    dates = list(record.get("dates") or [])
    idx, step = thin_indices(len(dates), points)
    data = dict(record)
    for key in ("dates", "total", "flip", "price", "fut"):
        seq = list(record.get(key) or [])
        data[key] = [seq[i] for i in idx] if seq else []
    # 嵌套对象解引用：缓存里的 record 会被多次复用，别让调用方改到缓存本身
    data["target"] = dict(record.get("target") or {})
    data["metrics"] = dict(record.get("metrics") or {})
    data["signs"] = dict(record.get("signs") or {})
    data["points"] = len(idx)
    data["thin"] = int(step)
    data["cached"] = bool(cached)
    data["computed_at"] = str(computed_at) if computed_at else None
    data["cache_age_s"] = None if cache_age_s is None else round(float(cache_age_s), 1)
    return data


def _chart_probe(thesis_id: str, defn: dict) -> dict | None:
    """便宜地探测「目标序列最后日期 + 频率 + 验证期数」（只读本地库，不联网补数）。

    存在的理由：缓存 key 含数据日，但完整 `_prepare()` 要 5~30s（会触发补数采集）。
    证据目标只有 index_daily（指数日线）与 macro（宏观/序列）两类，都能直接读库。
    这里**不重写目标解析**——同样调 `thesis.evidence.resolve_target`，只是喂一个
    只含目标别名的「轻 ctx」，保证探测口径与冷路径一致（实测 8 张台账端点日期
    全部一致；若补数后库前进一天，最多多算一次，下一次就会命中新 key）。
    探测不出来返回 None（调用方直接走冷路径）。
    """
    from thesis import evidence as thesis_evidence

    defn = defn or {}
    params = defn.get("params") or {}
    tgt = (defn.get("evidence") or {}).get("target") or {}
    alias = tgt.get("alias")
    ctx = {"series": {}, "frames": {}, "stale_days": {}}
    if alias:
        spec = (defn.get("data") or {}).get(alias) or {}
        kind = spec.get("kind")
        if kind == "index_valuation":
            # 派生码（idx_*）要 sync 后才知道，探测会联网 → 交给冷路径
            return None
        try:
            if kind == "index_daily":
                days = int(spec.get("days") or params.get("index_hist_days", 900))
                ctx["frames"][alias] = _default_engine().read_index_daily(
                    spec.get("code"), days)
            elif kind == "macro":
                from utils.cache import macro_db
                ctx["series"][alias] = macro_db.get_macro_series(
                    spec.get("code"), periods=int(spec.get("periods", 400)))
            else:
                # 别名不在 data 声明里 / kind 未知：实际解析也会走兜底，保持一致
                ctx = {"series": {}, "frames": {}, "stale_days": {}}
        except Exception as e:
            logger.debug("[Thesis] 双轴图目标探测失败(%s): %s", thesis_id, e)
            return None
    try:
        target = thesis_evidence.resolve_target(
            defn, ctx, fallback=_EVIDENCE_TARGET.get(thesis_id))
    except Exception as e:                 # 探测失败只是走慢路径，不该让请求失败
        logger.debug("[Thesis] 双轴图目标解析失败(%s): %s", thesis_id, e)
        return None
    dates = (target or {}).get("dates") or []
    if not dates:
        return None
    freq = target.get("freq") or "daily"
    return {"end": dates[-1], "freq": freq, "h": chart_horizon(freq)}


def _json_num(v):
    """numpy 标量 / NaN / inf → JSON 安全的 float | None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _factor_rows_for_cache(rows) -> list[dict]:
    """重放结果 → 可安全 `json.dumps` 的形状。

    `utils.cache.core._set` 用 `json.dumps(..., default=str)`：numpy.int64 /
    np.bool_ 这类不是 Python 内建类型的标量会被悄悄转成字符串（`3` → `"3"`），
    从 L2 读回来就废了。这里统一转 float/None（NaN→None，与「无观测」同义）。
    """
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append({"date": str(r.get("date") or ""),
                    "value": _json_num(r.get("value")),
                    "score": _json_num(r.get("score"))})
    return out


def _chart_factor_rows(thesis_id: str, def_hash: str, prep: dict, spec: dict,
                       tdates: list) -> tuple[list[dict], str]:
    """单个因子取重放序列（优先 L3 因子缓存），返回 (rows, 来源)。

    来源：`L1` 进程内命中 / `L2` 持久命中 / `replay` 本次重放 /
    `skip` 手工因子（没有可重放序列，与 replay_factor_series 同口径）。

    `input_end` 取 `max(因子输入轴)`——`factor_axis` 当前虽然返回升序，但**不要
    依赖排序**：轴乱序时 `axis[-1]` 会算错，进而把「有新数据」误判成「没变」。
    无轴（轴为空 / factor_axis 抛错）时不缓存，照常重放（宁可多算，不可错复用）。
    """
    from thesis import evidence as thesis_evidence

    compute = spec.get("compute")
    key = spec.get("key") or compute or "?"
    if not compute or compute == "manual":
        return [], "skip"

    axis: list = []
    try:
        axis = thesis_evidence.factor_axis(spec, prep.get("ctx") or {}) or []
    except Exception as e:              # noqa: BLE001 - 轴取不到只影响缓存，不影响重放
        logger.debug("[Thesis] 双轴图因子 %s 输入轴获取失败: %s", key, e)
    input_end = max(axis) if axis else ""
    if not input_end:
        return thesis_evidence.replay_factor_series(
            prep.get("engine"), spec, prep.get("ctx") or {}, prep.get("defn") or {},
            prep.get("params") or {}, tdates), "replay"

    ckey = chart_factor_cache_key(thesis_id, def_hash, key)
    now = time.monotonic()
    hit = _CHART_FACTOR_CACHE.get(ckey)
    if (hit and hit.get("input_end") == input_end
            and (now - float(hit.get("at") or 0.0)) <= CHART_FACTOR_CACHE_TTL_S):
        return hit.get("rows") or [], "L1"
    try:
        from utils.cache import api as cache_api
        l2 = cache_api.get_thesis_factor_cache(ckey)
    except Exception as e:              # noqa: BLE001 - 缓存故障只降级成重放
        l2 = None
        logger.debug("[Thesis] 因子 L2 读取失败(%s): %s", ckey, e)
    if (isinstance(l2, dict) and l2.get("input_end") == input_end
            and isinstance(l2.get("rows"), list)):
        _CHART_FACTOR_CACHE[ckey] = {"rows": l2["rows"], "input_end": input_end,
                                     "at": now}
        return l2["rows"], "L2"

    rows = thesis_evidence.replay_factor_series(
        prep.get("engine"), spec, prep.get("ctx") or {}, prep.get("defn") or {},
        prep.get("params") or {}, tdates)
    safe = _factor_rows_for_cache(rows)
    _CHART_FACTOR_CACHE[ckey] = {"rows": safe, "input_end": input_end, "at": now}
    try:
        from utils.cache import api as cache_api
        cache_api.set_thesis_factor_cache(
            ckey, {"rows": safe, "input_end": input_end},
            expire_hours=CHART_FACTOR_CACHE_L2_EXPIRE_HOURS)
    except Exception as e:              # noqa: BLE001 - 缓存故障只降级
        logger.debug("[Thesis] 因子 L2 写入失败(%s): %s", ckey, e)
    return safe, "replay"


def _chart_compute(thesis_id: str, prep: dict, target: dict, h: int) -> dict:
    """核心计算：重放因子 → ffill 到目标日轴 → 覆盖率面板 → 四条序列 + 指标。

    算法由 `.tmp/thesis_dual_axis.py` 产品化而来（那边是一次性脚本，这里改成
    纯函数 + 服务函数）。因子序列用 `thesis.evidence.replay_factor_series` 重放：
    与证据同口径、无前视；`total` 是评分和，`flip` 是按「因子 vs 未来收益」符号
    翻正后的和（内样本参考线，字段名由前端契约固定）。
    """
    import numpy as np

    from thesis import evidence as thesis_evidence

    defn = prep.get("defn") or {}
    tdates = list(target.get("dates") or [])
    closes = [float(x) for x in (target.get("values") or [])]
    if not tdates or not closes:
        raise ValueError("目标序列为空，无法生成双轴图")
    def_hash = chart_def_hash(defn)
    src_n = {"L1": 0, "L2": 0, "replay": 0, "skip": 0, "error": 0}

    # 1) 逐因子重放 + ffill 到目标日轴。重放失败/无序列/全空的因子进 dropped。
    #    每个因子先问 L3：因子输入轴 input_end 没前进就直接复用 rows（不 replay）。
    cols: list = []                 # [(key, arr, 首个有效位置)]
    factor_keys: list[str] = []     # 定义里的因子顺序，最终 dropped 按它排列
    for spec in defn.get("factors") or []:
        if not isinstance(spec, dict):
            continue
        key = spec.get("key") or spec.get("compute") or "?"
        factor_keys.append(key)
        try:
            rows, src = _chart_factor_rows(thesis_id, def_hash, prep, spec, tdates)
        except Exception as e:            # 单因子坏掉不能毁掉整张图
            logger.warning("[Thesis] 双轴图因子 %s 重放失败: %s", key, e)
            rows, src = [], "error"
        src_n[src] = src_n.get(src, 0) + 1
        arr = None
        if rows:
            scores = [np.nan if r.get("score") is None else float(r["score"]) for r in rows]
            cand = np.asarray(thesis_evidence.ffill_align(
                [r["date"] for r in rows], scores, tdates), dtype=float)
            if np.isfinite(cand).any():
                arr = cand
        if arr is None:
            logger.info("[Thesis] 双轴图丢弃因子 %s（%s 无可用评分序列）", key, thesis_id)
            continue
        cols.append((key, arr, int(np.where(np.isfinite(arr))[0][0])))

    # 2) 覆盖率面板：保留尽可能多的因子，但窗口必须够长
    panel = select_chart_panel([c[2] for c in cols], len(tdates))
    if panel is None:
        raise ValueError("可用因子过少或历史太短（不足 %d 期），无法生成双轴图"
                         % CHART_MIN_PERIODS)
    kept, start = panel
    kept_cols = [cols[i] for i in kept]
    kept_keys = {c[0] for c in kept_cols}
    dropped = [k for k in factor_keys if k not in kept_keys]

    # 3) 面板矩阵 → 总分 / 翻转总分 / 价格 / 未来 h 期收益
    idx = np.arange(start, len(tdates))
    S = np.vstack([c[1][idx] for c in kept_cols]).T
    ok = np.all(np.isfinite(S), axis=1)
    S = S[ok]
    if S.shape[0] < 2:
        raise ValueError("双轴图有效样本不足，无法生成")
    dates = [tdates[int(i)] for i in idx[ok]]
    price = np.asarray(closes, dtype=float)[idx][ok]
    fwd = np.asarray(thesis_evidence.forward_returns_at(
        dates, dates, [float(x) for x in price], h), dtype=float)
    total = S.sum(axis=1)
    flip, signs = flip_score_series(S, fwd)
    metrics = chart_metrics(total, price, fwd, np.asarray(flip, dtype=float))
    fut = fut_price_series(price, h)

    freq = target.get("freq") or "daily"
    from datetime import datetime

    # L3 命中统计（观测用：验收时能直接看到「这张图跳过了多少次 replay」）
    _LAST_FACTOR_STATS.clear()
    _LAST_FACTOR_STATS.update({
        "thesis_id": thesis_id, "def_hash": def_hash[:8],
        "L1": src_n["L1"], "L2": src_n["L2"], "replay": src_n["replay"],
        "skip": src_n["skip"], "error": src_n["error"],
        "dropped": len(dropped),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    logger.info("[Thesis] 双轴图因子缓存(%s)：L1 命中 %d / L2 命中 %d / 重放 %d / 手工跳过 %d",
                thesis_id, src_n["L1"], src_n["L2"], src_n["replay"], src_n["skip"])
    return {
        "thesis_id": thesis_id,
        "name": defn.get("name", thesis_id),
        "def_version": defn.get("version"),
        "def_hash": chart_def_hash(defn)[:8],
        "target": {
            "code": target.get("code") or "",
            "name": target.get("name") or "",
            "freq": freq,
            "freq_label": target.get("freq_label")
                           or thesis_evidence.freq_meta(freq).get("label") or freq,
        },
        "h": int(h),
        "n": int(S.shape[0]),
        "range": [dates[0], dates[-1]],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "signs": {c[0]: int(s) for c, s in zip(kept_cols, signs)},
        "dropped": dropped,
        "dates": dates,
        "total": [round(float(x), 4) for x in total],
        "flip": list(flip),
        "price": [round(float(x), 4) for x in price],
        "fut": [None if v is None else round(float(v), 4) for v in fut],
    }


def get_chart_data(thesis_id: str, force: bool = False,
                   points: int = CHART_DEFAULT_POINTS,
                   offline: bool = False) -> dict:
    """双轴复盘图数据：总分 / 翻转总分 / 标的价格 / 未来 h 期价格 + 相关性指标。

    口径（与 `.tmp/thesis_dual_axis.py` 对齐）：
    - 因子评分用 `thesis.evidence.replay_factor_series` 重放（与证据同口径、无前视），
      ffill 到目标日轴；覆盖率面板保证 >= CHART_MIN_PERIODS 期，起步过晚的因子进 dropped。
    - total = Σ评分；flip = 按各因子 vs 未来收益的 Spearman 符号翻正后求和（内样本参考线）。
    - price = 目标收盘；fut[i] = price[i+h]，尾部 h 个 None。
    - h：日 20 / 周 4 / 月 6；metrics 全部用**抽稀前**全量序列算。
    - n > points 时等步长抽稀（thin=步长），返回的四个数组与 dates 等长。

    三级缓存（细节见 docs/areas/thesis.md）：
    - **L1 进程内** `(thesis_id, def_hash, h)`，TTL 1h，**key 不含数据日**：
      1h 内即使目标序列前进一天也直接复用（用户要的是「研究版本没变就别重算」）；
      TTL 到期后先 `_chart_probe` 探测数据日：没变只续计时、不重算，前进才重建。
    - **L2 持久** `cache_thesis_chart:{tid}:{def_hash}:{h}`（cache_kv，TTL 1h）：
      跨进程复用（预热脚本写、web server 读）。L1 未命中先查 L2，命中回填 L1。
    - **L3 因子级** `cache_thesis_factor:{tid}:{def_hash}:{factor_key}`（TTL 24h）：
      `input_end`（因子输入轴最后日期）没前进就复用 rows，不 replay——新交易日
      只有真的前进的因子重算，月度/手工因子不动。
    `force=True` 跳过 L1/L2（仍复用 L3）；`refresh()` 成功后由
    `invalidate_chart_cache()` 清 L1/L2 并**保留 L3**（这就是「只算新增」）。

    `offline=True` 只读本地库（不补数、不抓估值），供 `refresh_background_data.py`
    在 `thesis_refresh` 之后预热：那一步数据刚拉过，实测 8 张从 ~344s 降到个位数秒。
    """
    from thesis import defs as thesis_defs

    defn, problems = thesis_defs.load_def_checked(thesis_id)
    if defn is None:
        raise ValueError("台账定义不可用：" + "；".join(problems))
    if problems:
        logger.warning("[Thesis] 双轴图定义存在问题(%s): %s", thesis_id, problems)

    points = max(2, min(int(points or CHART_DEFAULT_POINTS), CHART_MAX_POINTS))
    def_hash = chart_def_hash(defn)
    now = time.monotonic()

    # 探测数据日/频率（本地读库 ~10ms，不触发补数）。h 只取决于目标频率，
    # 探测拿到就用；探测失效时退回「上次算出的 h」提示——没有 h 拼不出 key。
    probe = None if force else _chart_probe(thesis_id, defn)
    hint = None if force else _CHART_HINT.get((thesis_id, def_hash))
    h = None
    if probe and probe.get("h"):
        h = int(probe["h"])
    elif hint and hint.get("h"):
        h = int(hint["h"])

    if not force and h is not None:
        key = chart_cache_key(thesis_id, def_hash, h)
        hit = _CHART_CACHE.get(key)
        # L1：1 小时内直接复用（即使数据日已前进）
        if hit and (now - float(hit.get("at") or 0.0)) <= CHART_CACHE_TTL_S:
            return _cached_chart_payload(hit, points)
        # L1 过期：探测数据日。没变 → 只续计时并复用（不重算），L2 同步续期
        probed_end = (probe or {}).get("end")
        if hit and probed_end and probed_end == hit.get("data_end"):
            hit["at"] = now
            _write_chart_l2(thesis_id, def_hash, h, hit)
            logger.debug("[Thesis] 双轴图 L1 续期 %s（数据日 %s 未变）",
                         thesis_id, probed_end)
            return _cached_chart_payload(hit, points)
        # L2：跨进程持久缓存（预热脚本写入 / 进程重启后仍有效）
        hit2 = _chart_l2_payload(thesis_id, def_hash, h, points)
        if hit2 is not None:
            return hit2

    # 冷路径：完整取数 + 重放（5~30s，含必要的联网补数；L3 让因子级只算新增）
    prep = _evidence_prep(thesis_id, offline=offline)
    if prep.get("error"):
        raise RuntimeError(prep["error"])
    target = prep.get("target")
    if not target:
        raise ValueError("目标标的不可用，无法生成双轴图")
    defn = prep.get("defn") or defn
    def_hash = chart_def_hash(defn)
    h = chart_horizon(target.get("freq") or "daily")
    data_end = (target.get("dates") or [None])[-1]
    key = chart_cache_key(thesis_id, def_hash, h)
    if not force:                   # 探测口径滞后一天时，用真实数据日兜住
        hit = _CHART_CACHE.get(key)
        if hit and (time.monotonic() - float(hit.get("at") or 0.0)) <= CHART_CACHE_TTL_S:
            return _cached_chart_payload(hit, points)
        # h 未知（探测与提示都没有）时上面没查过 L2，这里补查一次，避免白跑 prep
        hit2 = _chart_l2_payload(thesis_id, def_hash, h, points)
        if hit2 is not None:
            return hit2

    record = _chart_compute(thesis_id, prep, target, h)
    _chart_cache_prune()
    _chart_factor_prune()
    from datetime import datetime
    computed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"record": record, "data_end": data_end,
             "computed_at": computed_at, "at": time.monotonic()}
    _CHART_CACHE[key] = entry
    _write_chart_l2(thesis_id, def_hash, h, entry)
    # 记下「本次实际用到的数据日」：L2 命中/探测失效的台账下次靠它命中；旧定义版本的提示丢掉
    _CHART_HINT[(thesis_id, def_hash)] = {"end": record["range"][-1], "h": record["h"]}
    for k in [k for k in _CHART_HINT if k[0] == thesis_id and k[1] != def_hash]:
        _CHART_HINT.pop(k, None)
    logger.info("[Thesis] 双轴图计算完成 %s def=%s n=%d h=%d 因子=%d 丢弃=%s",
                thesis_id, record["def_hash"], record["n"], h,
                len(record["signs"]), record["dropped"])
    return chart_payload(record, points, cached=False,
                         computed_at=computed_at, cache_age_s=0.0)


def get_report_data(thesis_id: str, storage=None, force: bool = False) -> dict:
    """报告数据一次性组装（契约 5.4）：详情 + 历史 + 证据(details=1) + 归因 + 告警。

    **证据允许失败**：它是增强项，失败时给 `evidence.ok=False` 让报告缺证据区，
    而不是整份报告失败——用户要的「最近变动」「历史趋势」两块不依赖证据。

    30 分钟内复用同一份报告数据（`force=True` 绕过）。组装成本主要来自
    `engine._prepare()`（5~70s），同一张台账反复点「生成报告」不应每次重算；
    点「刷新」会通过 `invalidate_report_cache()` 主动失效。
    """
    from datetime import datetime

    from thesis import evidence as thesis_evidence

    storage = storage or _default_storage()
    ckey = (thesis_id, str(getattr(storage, "_db", "")))
    now = time.monotonic()
    if not force:
        hit = _REPORT_CACHE.get(ckey)
        if hit and (now - hit["at"]) <= REPORT_CACHE_TTL_S:
            data = dict(hit["data"])
            data["cache"] = {"hit": True, "force": False,
                             "age_s": round(now - hit["at"], 1),
                             "ttl_s": REPORT_CACHE_TTL_S}
            return data

    detail = get_detail(thesis_id, storage=storage)
    defn = detail.get("definition") or {}
    if not defn:
        raise ValueError("台账定义不存在: %s" % thesis_id)

    history = get_history(thesis_id, days=180, storage=storage)
    names = {f.get("key"): f.get("name")
             for f in (defn.get("factors") or []) if isinstance(f, dict)}
    try:
        evidence = get_all_factor_evidence(thesis_id, details=True)
    except Exception as e:
        logger.warning("[Thesis] 报告证据组装失败(%s): %s", thesis_id, e)
        evidence = {"thesis_id": thesis_id, "ok": False,
                    "reason": str(e)[:200], "rows": []}

    factors = [{"key": f.get("key"), "name": f.get("name"),
                "unit": f.get("unit", ""), "confidence": f.get("confidence")}
               for f in (defn.get("factors") or []) if isinstance(f, dict)]
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "thesis": {"id": defn.get("id", thesis_id),
                   "name": detail.get("name", thesis_id),
                   "asset_label": detail.get("asset_label", ""),
                   "description": defn.get("description", ""),
                   "caliber_note": defn.get("_说明_口径") or ""},
        "snapshot": detail.get("snapshot"),
        "history": history,
        "definition": {"factors": factors,
                       "signals": defn.get("signals") or {},
                       "invalidation": defn.get("invalidation") or {},
                       "thresholds_note": defn.get("_说明_阈值格式") or ""},
        "attribution": thesis_evidence.build_attribution(
            history.get("items") or [], names),
        "evidence": evidence,
        "alerts": detail.get("alerts") or [],
        "cache": {"hit": False, "force": bool(force), "age_s": 0,
                  "ttl_s": REPORT_CACHE_TTL_S},
    }
    # 时间戳记在**组装完成**时刻：组装本身可能 60s，用开始时刻会让 TTL 少算一截
    _REPORT_CACHE[ckey] = {"at": time.monotonic(), "data": data}
    return data


def ffill_factor(fac_dates, fac_vals, tgt_dates):
    """把因子序列映射到标的日期轴（取 ≤ 当日的最近因子值）。

    实现已收进 `thesis.evidence.ffill_align`；这里保留旧名字供既有调用点/测试使用。
    """
    from thesis.evidence import ffill_align
    return ffill_align(fac_dates, fac_vals, tgt_dates)


def _evidence_verdict(horizons: list) -> str:
    """把回测统计翻译成人话结论：这个因子到底有没有实际区分度。

    规则（取相关系数绝对值最大的那个期限作代表）：
    - |r| ≥ 0.3 且 Q5−Q1 ≥ 2pp → 支撑较强（高分位组明显跑赢/跑输低分位组）
    - |r| ≥ 0.15 → 有一定支撑
    - 其余 → 支撑弱（读数与未来收益基本无关，判断时不应依赖该因子）
    """
    hs = [h for h in horizons if h.get("corr") is not None]
    if not hs:
        return "样本不足，无法验证该因子与未来收益的关系"
    best = max(hs, key=lambda h: abs(h["corr"]))
    c, q = abs(best["corr"]), abs(best.get("q5_q1") or 0)
    sign = "正" if best["corr"] > 0 else "负"
    qq = best.get("q5_q1")
    if c >= 0.3 and q >= 2:
        return ("数据支撑较强：因子读数与未来 %d 日收益呈%s相关（r=%+.2f，n=%d），"
                "按读数分五组后最高组比最低组多 %.1fpp——该因子有实际区分度"
                % (best["h"], sign, best["corr"], best.get("n") or 0, abs(qq)))
    if c >= 0.15:
        return ("有一定支撑：因子读数与未来 %d 日收益呈%s相关（r=%+.2f，n=%d），"
                "分层差 %.1fpp，强度中等" % (best["h"], sign, best["corr"], best.get("n") or 0, abs(qq)))
    return ("数据支撑弱：因子读数与未来收益的相关接近 0（r=%+.2f，n=%d），"
            "分层差 %.1fpp——该因子暂无实际区分度，判断时请勿依赖"
            % (best["corr"], best.get("n") or 0, abs(qq)))
