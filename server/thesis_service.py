"""研究台账 Web 服务：刷新（计算 → 落定义 → 落快照 → 产告警）与读取。

放在 `server/` 而非 `thesis/` 的理由：这一层是 Web 编排（含降级话术、给前端的形状），
而 `thesis/` 包保持纯领域（engine/storage/alerts 都不依赖 FastAPI），
CLI 与技能可直接用领域层，不必绕 Web。

所有函数对「部分失败」保持宽容：单个台账坏定义不影响列表页，取数失败也返回降级形状
而不是 500——台账页最怕白屏。
"""
import logging

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

    # 台账数据已更新 → 清掉该台账的因子证据缓存：下次点开「数据验证」
    # 会用最新数据重算，而不是返回刷新前的旧结论
    invalidate_evidence_cache(thesis_id)

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

    # 组合风险：腿间相关性 + 等权组合波动 vs 两腿平均（把 basis 里的
    # 「分散化机制成立 / 波动降 17%」这类写死的文案变成可复算的读数）
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

    存在理由：`portfolio_map.json` 的 basis 里写着「两资产月度收益相关 +0.29」
    「50/50 波动较简单平均降 17%」这类**结论性常量**——写死之后没人能验证它们
    是不是还成立（相关性会漂）。这里把同一口径复算出来，让 basis 从「断言」变成
    「可复检的读数」。

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

    存在理由：原始研究里「越短窗口越偏负、越长越偏正」这类判断靠热力图一眼就能说清；
    台账原先只有单资产读数，读者无法横向对比，也无法验证「恒科波动是红利 2.1 倍」
    这类写死在文案里的常量。取不到的标的直接跳过并在 `missing` 里点名，不猜值。
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

# 多输入复合结构（矩阵 / 相对 / 手工）无法用单一序列回放 → 明确说明、不做假图
# 复合/手工因子无法用单一序列回放 → 明确说明、不做假图。
# style_relative 已支持（60 日滚动相对收益，对齐 red/base 两序列）。
_UNSUPPORTED_COMPUTES = {"manual", "dual_axis", "double_tightening"}

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


def _factor_series(engine, spec, ctx, params):
    """按 compute 类型构造因子的历史 (dates, vals)；构造不了返回 (None, None)。

    与引擎因子计算器的区别：这里要的是**逐期序列**（供散点/分层回测），
    而计算器只需要期末一个读数——但构造口径必须一致，否则证据是假的。
    """
    comp = spec.get("compute")

    def sd(alias):
        return [r["report_date"] for r in (ctx["series"].get(alias) or [])]

    def sv(alias):
        return [r["value"] for r in (ctx["series"].get(alias) or [])]

    if comp in ("rate_level_trend", "level_trend_pct"):
        return sd(spec["series"]), sv(spec["series"])

    if comp == "trend_bp":
        d, v = sd(spec["series"]), sv(spec["series"])
        n = int(spec.get("lookback_days", params.get("trend_lookback_days", 20)))
        scale = float(spec.get("scale", 100.0))
        if len(v) <= n:
            return None, None
        return d[n:], [round((v[i] - v[i - n]) * scale, 1) for i in range(n, len(v))]

    if comp == "spread_bp":
        da, va = sd(spec["a"]), sv(spec["a"])
        mb = dict(zip(sd(spec["b"]), sv(spec["b"])))
        scale = float(spec.get("scale", 1.0))
        od, ov = [], []
        for dd, a in zip(da, va):
            if dd in mb and a is not None and mb[dd] is not None:
                od.append(dd)
                ov.append(round((a - mb[dd]) * scale, 2))
        return od, ov

    if comp == "rate_premium":
        da, va = sd(spec["dy"]), sv(spec["dy"])
        mr = dict(zip(sd(spec["rate"]), sv(spec["rate"])))
        od, ov = [], []
        for dd, a in zip(da, va):
            if dd in mr and a is not None and mr[dd] is not None:
                od.append(dd)
                ov.append(round(a - mr[dd], 4))
        return od, ov

    if comp == "style_relative":
        df_red = ctx["frames"].get(spec["red"])
        df_base = ctx["frames"].get(spec["base"])
        if df_red is None or df_base is None:
            return None, None
        import numpy as np
        rd = [str(x)[:10] for x in df_red["trade_date"].tolist()]
        rc = dict(zip(rd, [float(x) for x in df_red["close"].tolist()]))
        bd = [str(x)[:10] for x in df_base["trade_date"].tolist()]
        bc = dict(zip(bd, [float(x) for x in df_base["close"].tolist()]))
        common = sorted(set(rc) & set(bc))
        lookback = int(params.get("style_lookback_days", 60))
        if len(common) <= lookback:
            return None, None
        out_d, out_v = [], []
        for i in range(lookback, len(common)):
            d = common[i]
            rr = (rc[d] / rc[common[i - lookback]] - 1) * 100
            br = (bc[d] / bc[common[i - lookback]] - 1) * 100
            out_d.append(d)
            out_v.append(round(rr - br, 2))
        return out_d, out_v

    if comp == "crowding":
        df = ctx["frames"].get(spec["red"])
        if df is None or len(df) < 120:
            return None, None
        import numpy as np
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]
        closes = np.array([float(x) for x in df["close"].tolist()])
        ma_days = int(params.get("ma_days", 750))
        ma_days = min(ma_days, len(closes) // 2)
        if ma_days < 60:
            return None, None
        ma = np.convolve(closes, np.ones(ma_days) / ma_days, mode="valid")
        dev = (closes[ma_days - 1:] / ma - 1) * 100
        return dates[ma_days - 1:], [round(float(x), 2) for x in dev]

    if comp == "index_shock":
        df = ctx["frames"].get(spec.get("frame"))
        if df is None or len(df) < 60:
            return None, None
        import numpy as np
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]
        closes = np.array([float(x) for x in df["close"].tolist()])
        h = int(spec.get("shock_lookback_days", params.get("shock_lookback_days", 5)))
        chg = (closes[h:] / closes[:-h] - 1) * 100
        return dates[h:], [round(float(x), 2) for x in chg]

    if comp == "volume_percentile":
        df = ctx["frames"].get(spec.get("frame"))
        if df is None or "amount" not in getattr(df, "columns", []):
            return None, None
        import numpy as np
        dates = [str(x)[:10] for x in df["trade_date"].tolist()]
        amts = np.array([float(x) if x else np.nan for x in df["amount"].tolist()])
        n = int(spec.get("volume_ma_days", params.get("volume_ma_days", 20)))
        if len(amts) <= n:
            return None, None
        # 分位是单调变换 → 直接用原始 20 日均额回测（相关/分层结论等价）
        ma = np.array([np.nanmean(amts[max(0, i - n + 1):i + 1]) for i in range(len(amts))])
        return dates[n - 1:], [round(float(x), 2) for x in ma[n - 1:]]

    return None, None


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


def _evidence_prep(thesis_id: str) -> dict:
    """证据计算的**共享准备**：定义 + 引擎 + ctx + 目标标的日线。

    单独抽出来的理由：`engine._prepare()` 会触发取数补数（实测 ~25s）。
    单因子点开时跑一次无所谓；批量校验一张台账的全部因子时，逐因子重跑会
    线性累加，故由调用方准备一次、全因子复用。
    """
    from thesis import defs as thesis_defs

    defn, _ = thesis_defs.load_def_checked(thesis_id)
    if defn is None:
        return {"error": "台账定义不可用"}
    engine = _default_engine()
    params = defn.get("params") or {}
    ctx = engine._prepare(defn, params, [])
    tgt_code, tgt_name = _EVIDENCE_TARGET.get(thesis_id, ("", ""))
    df = None
    if tgt_code:
        try:
            from utils.cache.market_data_db import get_index_daily
            df = get_index_daily(tgt_code)
        except Exception as e:
            logger.warning("[Thesis] 证据目标日线读取失败(%s): %s", tgt_code, e)
    return {"defn": defn, "engine": engine, "params": params, "ctx": ctx,
            "tgt_code": tgt_code, "tgt_name": tgt_name, "df": df}


def _compute_factor_evidence(thesis_id: str, factor_key: str, prep: dict | None = None) -> dict:
    """单因子的关联性证据：因子历史序列 vs 标的未来 20/60 日收益。

    输出散点（方向与强度）+ 五分位分层（Q1 最低组 vs Q5 最高组的收益差），
    回答「凭什么这个因子在这张卡上」。**不做假图**：复合因子（双轴矩阵/相对
    强弱/手工项）与样本不足的，一律返回 supported=False 并说明原因。

    `prep` 为 `_evidence_prep()` 的结果；批量调用时传入以复用 ctx（省掉逐因子补数）。
    """
    import numpy as np

    prep = prep or _evidence_prep(thesis_id)
    if prep.get("error"):
        return {"supported": False, "reason": prep["error"]}

    defn = prep["defn"]
    spec = next((f for f in defn.get("factors") or []
                 if f.get("key") == factor_key), None)
    if spec is None:
        return {"supported": False, "reason": "因子不存在: %s" % factor_key}
    if spec.get("compute") in _UNSUPPORTED_COMPUTES:
        return {"supported": False, "reason": "复合/手工因子，无单一时间序列可回放",
                "factor_name": spec.get("name", factor_key)}

    engine, params, ctx = prep["engine"], prep["params"], prep["ctx"]

    dates, vals = _factor_series(engine, spec, ctx, params)
    if not dates or len(dates) < 120:
        return {"supported": False, "reason": "因子序列样本不足（%s 期）" % len(dates or []),
                "factor_name": spec.get("name", factor_key)}

    tgt_code, tgt_name, df = prep["tgt_code"], prep["tgt_name"], prep["df"]
    if not tgt_code:
        return {"supported": False, "reason": "未配置目标标的"}
    if df is None or len(df) < 120:
        return {"supported": False, "reason": "标的（%s）无日线数据" % tgt_name}

    tgt_dates = [str(x)[:10] for x in df["trade_date"].tolist()]
    tgt_closes = np.array([float(x) for x in df["close"].tolist()])
    fv = ffill_factor(dates, vals, tgt_dates)

    horizons, scatter = [], None
    for h in (20, 60):
        fut = np.full(len(tgt_closes), np.nan)
        fut[:-h] = (tgt_closes[h:] / tgt_closes[:-h] - 1) * 100
        mask = np.isfinite(fv) & np.isfinite(fut)
        x, y = fv[mask], fut[mask]
        if len(x) < 60:
            horizons.append({"h": h, "n": int(len(x)), "note": "样本不足"})
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        qs = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
        bounds = [-np.inf, *qs, np.inf]
        layers = []
        for k, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:])):
            m = (x > lo) & (x <= hi) if k > 0 else (x <= hi)
            layers.append(round(float(np.nanmean(y[m])), 2))
        horizons.append({"h": h, "n": int(len(x)), "corr": round(r, 3),
                         "q5_q1": round(layers[-1] - layers[0], 2), "layers": layers})
        if h == 20:
            scatter = [[round(float(a), 4), round(float(b), 3)] for a, b in zip(x, y)]

    return {"supported": True, "factor_key": factor_key,
            "factor_name": spec.get("name", factor_key),
            "target": {"code": tgt_code, "name": tgt_name},
            "series_start": dates[0] if dates else "",
            "verdict": _evidence_verdict(horizons),
            "horizons": horizons, "scatter": scatter or []}


def _best_horizon(ev: dict) -> dict | None:
    """取相关系数绝对值最大的期限（20/60 日）作代表。"""
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
    c, q = abs(best["corr"]), abs(best.get("q5_q1") or 0)
    if c >= 0.3 and q >= 2:
        return "strong"
    if c >= 0.15:
        return "medium"
    return "weak"


def get_all_factor_evidence(thesis_id: str) -> dict:
    """一张台账**全部因子**的有效性汇总（因子有效性回测的产品化入口）。

    存在理由：单因子证据要「点开某个因子」才看得到，回答不了「这张台账的因子
    有几个真有区分度、有几个是凑数的」。新增台账 / 新增因子的**准入检查**需要的
    正是这个横向视图——否则每建一张台账都得逐因子手工点一遍。

    与 `get_factor_evidence` 的差别：ctx 只准备一次（见 `_evidence_prep`），
    因此批量明显快于逐因子串行；同时**不逐因子按天缓存**（汇总口径不同）。
    """
    prep = _evidence_prep(thesis_id)
    if prep.get("error"):
        return {"thesis_id": thesis_id, "ok": False, "reason": prep["error"], "rows": []}
    defn = prep["defn"]
    rows = []
    tiers = {"strong": 0, "medium": 0, "weak": 0, "insufficient": 0, "unsupported": 0}
    for spec in defn.get("factors") or []:
        key = spec.get("key")
        ev = _compute_factor_evidence(thesis_id, key, prep=prep)
        tier = _evidence_tier(ev)
        tiers[tier] += 1
        best = _best_horizon(ev) or {}
        rows.append({
            "factor_key": key,
            "factor_name": ev.get("factor_name") or spec.get("name") or key,
            "tier": tier,
            "supported": bool(ev.get("supported")),
            "reason": ev.get("reason"),
            "h": best.get("h"), "corr": best.get("corr"),
            "q5_q1": best.get("q5_q1"), "n": best.get("n"),
            "verdict": ev.get("verdict") or ev.get("reason"),
            "confidence": (spec.get("confidence") or {}).get("level"),
        })
    return {"thesis_id": thesis_id, "ok": True,
            "name": defn.get("name", thesis_id),
            "target": {"code": prep["tgt_code"], "name": prep["tgt_name"]},
            "summary": tiers,
            "usable": tiers["strong"] + tiers["medium"],
            "verifiable": len(rows) - tiers["unsupported"],
            "factor_count": len(rows), "rows": rows}


def ffill_factor(fac_dates, fac_vals, tgt_dates):
    """把因子序列映射到标的日期轴（取 ≤ 当日的最近因子值）。"""
    import numpy as np
    out = np.full(len(tgt_dates), np.nan)
    j = -1
    for i, d in enumerate(tgt_dates):
        while j + 1 < len(fac_dates) and fac_dates[j + 1] <= d:
            j += 1
        if j >= 0 and fac_dates[j] <= d:
            out[i] = fac_vals[j]
    return out


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
