"""研究台账：因子历史重放 + 关联性证据（纯函数优先，便于单测）。

存在理由
--------
台账页原先只能回答「今天总分 +4」，答不了用户真正问的两件事：
① 今天涨的这一分是哪个因子给的；② 这个因子和标的到底正相关还是负相关、
什么时候反转。旧实现（`server/thesis_service._factor_series`）为 9 种 compute
各写了一套历史序列构造，口径与引擎计算器**已经漂移**，而且 gold/copper/hog
的多数因子根本构造不出来 → 直接 unsupported，页面上只能说「结构不可测」。

本模块的做法：**不另写因子公式**。对每个采样日期 d，把 ctx 截断成
`report_date <= d` / `trade_date <= d`，再调用引擎注册表里**同一个**计算器
（`ThesisEngine.FACTOR_COMPUTERS[compute]`），取它输出的读数。这样历史序列
与页面当期读数天然同源，也不会漏掉没写进旧 `_factor_series` 的 compute 类型。

四条硬约束（与设计文档 §4 一致）
--------------------------------
- **无前视**：截断只保留 ≤d 的数据；未来收益取 d 之后 h 期。
- **只用收益做相关**：目标标的先算 h 期收益率，再与因子读数/分数求相关，
  绝不用价格水平直接算 Pearson（价格非平稳，会假性高相关）。
- **数据缺失 ≠ 0**：取不到一律 None + 原因；样本不足直接 `supported=False`，
  不画假图、不填 0。
- **口径与生产一致**：历史值来自生产计算器，不在此模块重写任何因子逻辑。

频率自适应
----------
按目标标的日期间隔中位数判日/周/月频，未来收益与滚动窗口都随频率缩放
（日频 20/60 个交易日、周频 4/12 周、月频 1/3 个月）——同一套代码同时服务
日频指数、周频猪价与月频宏观序列。
"""
import bisect
import logging
import math
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 输出抽稀上限（滚动曲线 / 散点 / 双轴）：证据图与 JSON 体积控制用，不影响统计。
MAX_POINTS = 240

# 重放采样上限：采样轴是**因子输入数据的日期轴**（不是目标日期轴）。
# 目标轴可能比因子长得多（指数 900 个交易日 vs 股息率只有 49 个交易日），
# 按目标轴等距采样会把稀疏因子的观测点大半个漏掉，最后误判成「样本不足」。
MAX_REPLAY_POINTS = 600

# 少于这个样本数不给结论（曲线还在长，等得起；给了反而被当成可信统计量）
MIN_SAMPLES = 30

# 单个期限上做相关/分层所需的最少样本
MIN_HORIZON_SAMPLES = 30

# 频率 → 期限 / 滚动窗口 / 「较 N 期前」的 N。改这里等于改全部结论口径。
# horizons 与 window 都按**目标标的**的频率取：未来收益是目标价的 h 个观测，
# 滚动窗口同理——采样间隔（长序列被抽稀后约 4~7 天一个点）不是频率，
# 拿采样间隔判 freq 会把窗口缩到 12 期、曲线剧烈抖动（实测踩过）。
# lag 按**因子观测**的频率取（"较 N 期前"数的是页面上看得到的 N 个观测）。
_FREQ = {
    "daily": {"label": "日频",
              "horizons": [(20, "未来 20 个交易日"), (60, "未来 60 个交易日")],
              "window": 60, "lag": 20},
    "weekly": {"label": "周频",
               "horizons": [(4, "未来 4 周"), (12, "未来 12 周")],
               "window": 12, "lag": 4},
    "monthly": {"label": "月频",
                "horizons": [(1, "未来 1 个月"), (3, "未来 3 个月")],
                "window": 6, "lag": 1},
}

# 常规期限样本都不足时追加的短窗口：只作方向参考（收益重叠更严重），
# 但它让「刚积累一个多月的因子」也能给出一个 n≥30 的弱结论，而不是一片空白。
_SHORT_HORIZON = {
    "daily": (5, "未来 5 个交易日"),
    "weekly": (1, "未来 1 周"),
    "monthly": None,
}

# 阈值表单调性 → 方向：|corr| 低于该值视为「弱」，不做方向判定
_WEAK_CORR = 0.15

# 离散档位因子：这些 compute 的读数本身就是矩阵得分（值 == 分）
_DISCRETE_COMPUTES = {"dual_axis", "double_tightening"}

_CAVEATS = [
    "相关不等于因果：因子只是与未来收益同向/反向，不能据此判断因果机制。",
    "未来收益窗口重叠：相邻样本共享大部分区间，有效独立样本远少于 n。",
    "价格序列非平稳：全样本相关可能被趋势主导；滚动相关、分位与区间均为样本内统计，需样本外验证。",
]


# ── 小工具 ────────────────────────────────────────────────────────────

def _to_date_str(x) -> str:
    """统一成 YYYY-MM-DD 字符串（库里可能是 datetime/date/Timestamp）。"""
    return str(x)[:10] if x is not None else ""


def _finite(v) -> bool:
    try:
        return bool(np.isfinite(float(v)))
    except (TypeError, ValueError):
        return False


def _round(v, nd: int = 3):
    """JSON 安全的四舍五入：NaN/inf → None（前端不认 NaN）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _fmt_num(v) -> str:
    """标签用的紧凑数字（2.5542 → '2.5542'，100.0 → '100'）。"""
    if v is None:
        return "…"
    s = ("%.4f" % float(v)).rstrip("0").rstrip(".")
    return s or "0"


# ── 频率判定 ──────────────────────────────────────────────────────────

def detect_freq(dates) -> str:
    """按日期间隔**中位数**判日/周/月频。

    用中位数而不是平均数：序列里常有停牌/长假造成的长空档，平均数会被拉大，
    把日频误判成周频，进而让「未来 20 日收益」变成「未来 20 周收益」。
    """
    ds = sorted({_to_date_str(x) for x in (dates or []) if x})
    if len(ds) < 3:
        return "daily"
    gaps = []
    for a, b in zip(ds, ds[1:]):
        try:
            gaps.append((date.fromisoformat(b) - date.fromisoformat(a)).days)
        except ValueError:
            continue
    if not gaps:
        return "daily"
    gaps.sort()
    med = gaps[len(gaps) // 2]
    if med <= 3:
        return "daily"
    if med <= 10:
        return "weekly"
    return "monthly"


def freq_meta(freq: str | None) -> dict:
    return _FREQ.get(freq or "daily") or _FREQ["daily"]


# ── 目标标的解析 ──────────────────────────────────────────────────────

def _default_index_loader(code: str):
    from utils.cache.market_data_db import get_index_daily
    return get_index_daily(code)


def _mk_target(dates, values, *, code="", name="", alias="", kind="frame", unit=""):
    rows = []
    for d, v in zip(dates or [], values or []):
        if not _finite(v):
            continue
        rows.append((_to_date_str(d), float(v)))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    ds = [r[0] for r in rows]
    vs = [r[1] for r in rows]
    freq = detect_freq(ds)
    return {"code": code or "", "name": name or "", "alias": alias or "",
            "kind": kind, "unit": unit or "", "dates": ds, "values": vs,
            "freq": freq, "freq_label": freq_meta(freq)["label"], "n": len(ds)}


def target_from_frame(df, *, code="", name="", alias="", unit="", kind="frame"):
    """DataFrame（trade_date/report_date + close）→ 目标标的序列。"""
    if df is None or len(df) == 0 or "close" not in getattr(df, "columns", []):
        return None
    col = "trade_date" if "trade_date" in df.columns else (
        "report_date" if "report_date" in df.columns else None)
    if col is None:
        return None
    return _mk_target(df[col].tolist(), df["close"].tolist(),
                      code=code, name=name, alias=alias, kind=kind, unit=unit)


def resolve_target(defn, ctx, fallback=None, index_loader=None):
    """解析「关联性证据的目标标的」。

    优先级（与设计文档 §5 对齐）：
    1. 定义里的 `evidence.target`（kind=frame/series + alias）——最权威，
       因为它声明的是「这条台账的读数到底对应哪个可交易标的」；
    2. 服务层 `_EVIDENCE_TARGET` 硬编码映射（老台账的兼容回退）；
    3. `subject.index_code` 走 index_daily（最后兜底）。

    取不到返回 None（调用方给 supported=False + 原因，不猜代理标的）。
    """
    defn = defn or {}
    ctx = ctx or {}
    ev = defn.get("evidence") or {}
    tgt = ev.get("target") or {}
    subject = defn.get("subject") or {}
    kind = tgt.get("kind")
    alias = tgt.get("alias")
    code = tgt.get("code") or subject.get("index_code") or ""
    name = tgt.get("name") or subject.get("index_name") or ""
    unit = tgt.get("unit") or ""

    if kind == "frame" and alias:
        got = target_from_frame((ctx.get("frames") or {}).get(alias),
                                code=code, name=name, alias=alias, unit=unit)
        if got:
            return got
    if kind == "series" and alias:
        rows = ((ctx.get("series") or {}).get(alias)) or []
        got = _mk_target([r.get("report_date") for r in rows],
                         [r.get("value") for r in rows],
                         code=code, name=name, alias=alias, kind="series", unit=unit)
        if got:
            return got

    # 回退 1/2：硬编码映射 → subject.index_code，都走 index_daily（本地库，不联网）
    candidates = []
    if fallback:
        if isinstance(fallback, (tuple, list)) and fallback:
            candidates.append((str(fallback[0]), fallback[1] if len(fallback) > 1 else ""))
        else:
            candidates.append((str(fallback), ""))
    if subject.get("index_code"):
        candidates.append((str(subject["index_code"]), subject.get("index_name") or ""))
    loader = index_loader or _default_index_loader
    for code2, name2 in candidates:
        if not code2:
            continue
        try:
            df = loader(code2)
        except Exception as e:      # 取不到只是没有证据，不能让台账页整体失败
            logger.warning("[Thesis] 证据目标日线读取失败(%s): %s", code2, e)
            df = None
        got = target_from_frame(df, code=code2, name=name2 or name,
                               alias="", unit="点")
        if got:
            return got
    return None


# ── 历史重放（无前视） ────────────────────────────────────────────────

def _frame_date_strings(ctx, alias, df):
    """按 ctx 缓存 K 线的日期字符串：重放会对同一份 ctx 切片上百次。"""
    cache = ctx.get("_date_cache")
    if cache is None:
        cache = ctx["_date_cache"] = {}
    got = cache.get(alias)
    if got is None or len(got) != len(df):
        got = cache[alias] = [_to_date_str(x) for x in df["trade_date"].tolist()]
    return got


def _series_date_strings(ctx, alias, rows):
    """序列（长表）的日期字符串缓存：返回 (日期列表, 是否升序)。"""
    cache = ctx.get("_date_cache")
    if cache is None:
        cache = ctx["_date_cache"] = {}
    key = "series:" + str(alias)
    got = cache.get(key)
    if got is None or got[0] != len(rows):
        dates = [_to_date_str(r.get("report_date")) for r in rows]
        sorted_ok = all(a <= b for a, b in zip(dates, dates[1:]))
        got = cache[key] = (len(rows), dates, sorted_ok)
    return got[1], got[2]


def slice_ctx(ctx, cutoff: str) -> dict:
    """把 ctx 截断到 `<= cutoff`：这是「无前视」的唯一实现点。

    序列按 report_date 截、K 线按 trade_date 截；`stale_days` 原样带过去
    （陈旧判定与是否前视无关）。返回新 dict，不改调用方 ctx。

    升序序列用二分定位后切片（列表切片是 C 层操作）；只有非升序数据才退回
    逐行过滤——重放要切片上百次，这一步的常数直接决定证据计算的整体耗时。
    """
    cutoff = _to_date_str(cutoff)
    ctx = ctx or {}
    series = {}
    for alias, rows in (ctx.get("series") or {}).items():
        rows = rows or []
        dates, sorted_ok = _series_date_strings(ctx, alias, rows)
        if sorted_ok:
            series[alias] = rows[:bisect.bisect_right(dates, cutoff)]
        else:
            series[alias] = [r for r in rows
                             if _to_date_str(r.get("report_date")) <= cutoff]
    frames = {}
    for alias, df in (ctx.get("frames") or {}).items():
        if df is None or "trade_date" not in getattr(df, "columns", []):
            frames[alias] = df
            continue
        dates = _frame_date_strings(ctx, alias, df)
        k = bisect.bisect_right(dates, cutoff)
        frames[alias] = df.iloc[:k].reset_index(drop=True) if k < len(dates) else df
    return {"series": series, "frames": frames,
            "stale_days": dict(ctx.get("stale_days") or {})}


def _fingerprint(sliced: dict):
    """截断后各数据源的「最新日期」指纹。

    相邻采样日若没有任何数据更新（月频指标在日频轴上很常见），计算器输出
    必然与上一次相同，直接复用即可——省掉大部分重复计算，结果完全一致。
    """
    sig = []
    for a in sorted(sliced.get("series") or {}):
        rows = sliced["series"][a]
        sig.append((a, _to_date_str(rows[-1].get("report_date")) if rows else ""))
    for a in sorted(sliced.get("frames") or {}):
        df = sliced["frames"][a]
        last = ""
        try:
            if df is not None and len(df) and "trade_date" in df.columns:
                last = _to_date_str(df["trade_date"].tolist()[-1])
        except Exception:
            last = ""
        sig.append((a, last))
    return tuple(sig)


def _sample_axis(target_dates, max_points: int) -> list[str]:
    """等距采样轴：不超过 max_points，且**必含最后一天**。

    最后一天必须在内：否则「当前读数」在滚动相关里会缺一格，前端看起来像
    「证据停在几天前」。
    """
    ds = sorted({_to_date_str(x) for x in (target_dates or []) if x})
    n = len(ds)
    if n == 0:
        return []
    if max_points <= 0 or n <= max_points:
        return ds
    step = max(1, math.ceil(n / max_points))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    if len(idx) > max_points:
        idx = idx[:max_points - 1] + [n - 1]
    return [ds[i] for i in idx]


def factor_score(spec, value, thresholds: dict, fallback=None):
    """因子值 → 评分（按 spec["thresholds_key"] 查定义阈值表）。

    查不到就用计算器自己给的 score；再退一步用 value 本身——离散档位因子
    （`dual_axis` / `double_tightening`）的读数就是分数，不存在第二套口径。
    """
    key = (spec or {}).get("thresholds_key")
    table = (thresholds or {}).get(key) if key else None
    if table:
        from thesis.defs import score_by_table
        score = score_by_table(value, table)
        if score is not None:
            return score
    if fallback is not None and _finite(fallback):
        return float(fallback)
    return value


def factor_axis(spec, ctx) -> list[str]:
    """因子**输入数据**的日期并集 —— 重放采样轴。

    为什么不能用目标标的的日期轴：目标可能比因子长得多（指数 900 个交易日、
    指数股息率只有近 49 个交易日）。按目标轴等距采样会漏掉大部分因子观测
    （521 个目标日 / 每 3 天采一个 → 只剩 15 个点），最后把「数据稀疏」误判成
    「样本不足」。这里按 spec 里出现的别名收集真实数据日期，只采因子真的有
    观测的那些天。

    做法刻意宽松：spec 里**任何**字符串值只要命中 ctx 的别名就计入（别名与
    metric 同名时是同一份数据，重复无害）。新增计算器即便换了字段名，只要还
    引用 ctx 别名就自动被覆盖。
    """
    series = (ctx or {}).get("series") or {}
    frames = (ctx or {}).get("frames") or {}
    dates = set()
    for val in (spec or {}).values():
        names = [val] if isinstance(val, str) else (
            val if isinstance(val, (list, tuple)) else [])
        for name in names:
            if not isinstance(name, str):
                continue
            if name in series:
                for r in series[name] or []:
                    if _finite(r.get("value")):
                        dates.add(_to_date_str(r.get("report_date")))
            elif name in frames:
                df = frames[name]
                if df is not None and "trade_date" in getattr(df, "columns", []):
                    dates.update(_to_date_str(x) for x in df["trade_date"].tolist())
    return sorted(d for d in dates if d)


def replay_factor_series(engine, spec, ctx, defn, params, target_dates=None,
                         max_points: int = MAX_REPLAY_POINTS) -> list[dict]:
    """逐采样日重放因子历史序列（口径与生产计算器完全一致）。

    每个采样日 d：ctx 截断到 d → 调 `engine.FACTOR_COMPUTERS[compute]` →
    取 factor["value"]/factor["score"]。`compute=manual` 直接返回空
    （手工因子由快照累积值兜底，见 `snapshot_series`），未注册/异常/None 跳过。

    采样轴 = 因子输入数据的日期并集（见 `factor_axis`），`target_dates` 只在
    因子输入完全取不到时兜底。返回 [{"date","value","score"}]（升序）；
    无前视是本函数的核心约束。
    """
    spec = spec or {}
    compute = spec.get("compute")
    if not compute or compute == "manual":
        return []
    computers = getattr(engine, "FACTOR_COMPUTERS", None) or {}
    fn = computers.get(compute)
    if fn is None:
        return []
    axis = factor_axis(spec, ctx) or list(target_dates or [])
    if not axis:
        return []
    thresholds = (defn or {}).get("thresholds") or {}
    out: list[dict] = []
    last_sig, last = None, None
    for d in _sample_axis(axis, max_points):
        sliced = slice_ctx(ctx, d)
        sig = _fingerprint(sliced)
        if sig == last_sig and last is not None:
            out.append({"date": d, "value": last["value"], "score": last["score"]})
            continue
        try:
            factor, _ = fn(engine, spec, sliced, defn, params, thresholds, [])
        except Exception as e:      # 单个日期算失败不能毁掉整条序列
            logger.debug("[Thesis] 因子 %s 在 %s 重放失败: %s",
                         spec.get("key"), d, e)
            last_sig, last = None, None
            continue
        last_sig = sig
        if not isinstance(factor, dict) or not _finite(factor.get("value")):
            last = None
            continue
        val = float(factor["value"])
        last = {"date": d, "value": val,
                "score": factor_score(spec, val, thresholds, factor.get("score"))}
        out.append(last)
    return out


def snapshot_series(history, factor_key: str) -> list[dict]:
    """每日快照累积的因子序列（手工/不可回放因子的证据来源）。"""
    rows = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        v = ((item.get("values") or {}).get(factor_key))
        if not _finite(v):
            continue
        rows.append({"date": _to_date_str(item.get("trade_date")),
                     "value": float(v),
                     "score": (item.get("scores") or {}).get(factor_key)})
    rows.sort(key=lambda r: r["date"])
    return rows


# ── 统计工具（纯 numpy，无外部统计依赖） ──────────────────────────────

def ffill_align(fac_dates, fac_vals, axis_dates):
    """把因子序列映射到目标日期轴：取 ≤ 当日的最近因子值（前向填充）。"""
    out = np.full(len(axis_dates), np.nan)
    j = -1
    for i, d in enumerate(axis_dates):
        while j + 1 < len(fac_dates) and fac_dates[j + 1] <= d:
            j += 1
        if j >= 0 and _finite(fac_vals[j]):
            out[i] = float(fac_vals[j])
    return out


def _price_array(closes):
    """价格序列 → 干净的 float ndarray（非数值/NaN 统一成 NaN）。"""
    src_ = closes if closes is not None else []
    try:
        px = np.asarray(src_, dtype=float)          # list / ndarray 走快路
    except (TypeError, ValueError):                 # 含 None 等脏值才逐点清洗
        px = np.array([float(x) if _finite(x) else np.nan for x in src_], dtype=float)
    return np.where(np.isfinite(px), px, np.nan)


def forward_returns(closes, h: int):
    """未来 h 期收益率（%，不含最后 h 个点）——**目标日轴**口径。"""
    px = _price_array(closes)
    out = np.full(len(px), np.nan)
    if h > 0 and len(px) > h:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[:-h] = (px[h:] / px[:-h] - 1.0) * 100.0
        out[~np.isfinite(out)] = np.nan
    return out


def forward_returns_at(fdates, tgt_dates, closes, h: int):
    """按**因子观测日**取未来 h 个目标观测的收益（%）。

    证据统计的样本轴就是它：对每个因子日期 d，目标价起点取「≤d 的最后一个
    目标观测」，再往后 h 个观测算收益。稀疏因子（月频宏观、只有近 49 个交易日
    的指数股息率）因此拥有**自己的 n**，不会被 ffill 成几百个伪独立样本
    （那会让 r 的显著性、分层 n、滚动窗口一起失真）。
    尾部不足 h 个观测的留 NaN——不猜、不前视。
    """
    tgt = list(tgt_dates or [])
    px = _price_array(closes)
    out = np.full(len(fdates), np.nan)
    if not tgt or h <= 0:
        return out
    for k, d in enumerate(fdates):
        j = bisect.bisect_right(tgt, _to_date_str(d)) - 1
        if j < 0 or j + h >= len(px):
            continue
        a, b = px[j], px[j + h]
        if np.isfinite(a) and np.isfinite(b) and a != 0:
            out[k] = (b / a - 1.0) * 100.0
    return out


def pearson(x, y):
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    m = np.isfinite(xs) & np.isfinite(ys)
    if m.sum() < 3:
        return None
    xs, ys = xs[m], ys[m]
    if np.std(xs) == 0 or np.std(ys) == 0:
        return None
    return float(np.corrcoef(xs, ys)[0, 1])


def spearman(x, y):
    """Spearman 秩相关（平均秩处理并列值，不引入 scipy 依赖）。"""
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    m = np.isfinite(xs) & np.isfinite(ys)
    if m.sum() < 3:
        return None
    rx = pd.Series(xs[m]).rank(method="average").to_numpy(dtype=float)
    ry = pd.Series(ys[m]).rank(method="average").to_numpy(dtype=float)
    return pearson(rx, ry)


def rolling_corr(x, y, window: int):
    """滚动 Pearson：返回与输入等长的数组（窗口未满/样本不够处为 NaN）。

    窗口内有效点少于 `max(10, window//2)` 时留空——半窗数据算出来的相关系数
    会剧烈跳动，比不给更误导。
    """
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    n = len(xs)
    out = np.full(n, np.nan)
    if window < 3 or n < window:
        return out
    need = max(10, window // 2)
    for i in range(window - 1, n):
        a = xs[i - window + 1:i + 1]
        b = ys[i - window + 1:i + 1]
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() < need:
            continue
        r = pearson(a[m], b[m])
        if r is not None:
            out[i] = r
    return out


def corr_phases(dates, corrs, min_len: int = 3) -> list[dict]:
    """把滚动相关切成正/负相位（连续同号区间），并给出每段均值与长度。

    「什么时候反相关」是用户问题的核心：只给全样本 r 看不出相关性会反转。

    `min_len`：长度不足的相位并入相邻较长段。滚动窗口本身有平滑滞后，
    一两期就翻回来的「反转」几乎都是估计噪声；不合并的话相位列表会碎成
    几十段（实测 hog 47 段），反而看不出真正的 regime 切换。
    """
    out: list[dict] = []
    cur = None
    for d, v in zip(list(dates or []), list(corrs) if corrs is not None else []):
        if not _finite(v):
            if cur:
                out.append(cur)
                cur = None
            continue
        f = float(v)
        sign = "positive" if f > 0 else ("negative" if f < 0 else "neutral")
        if cur is not None and cur["sign"] == sign:
            cur["end"] = d
            cur["_vals"].append(f)
        else:
            if cur:
                out.append(cur)
            cur = {"start": d, "end": d, "sign": sign, "_vals": [f]}
    if cur:
        out.append(cur)

    # 合并过短相位：优先并入相邻更长的段（等长时并入前一段）。
    # 只有「确实存在足够长的段」时才合并——全都很短说明这条序列本身没有
    # 稳定的相关区间，此时硬合并会把噪声拼成一个假的「负相关 phase」。
    min_len = max(1, int(min_len or 1))
    merged = bool(out) and max(len(seg["_vals"]) for seg in out) >= min_len
    while merged and len(out) > 1:
        merged = False
        for i, seg in enumerate(out):
            if len(seg["_vals"]) >= min_len:
                continue
            if i == 0:
                j = 1
            elif i == len(out) - 1:
                j = i - 1
            else:
                j = i - 1 if len(out[i - 1]["_vals"]) >= len(out[i + 1]["_vals"]) else i + 1
            tgt = out[j]
            if j < i:               # 并入前一段：短段在后
                tgt["_vals"] = tgt["_vals"] + seg["_vals"]
            else:                   # 并入后一段：短段在前
                tgt["_vals"] = seg["_vals"] + tgt["_vals"]
            tgt["start"] = min(tgt["start"], seg["start"])
            tgt["end"] = max(tgt["end"], seg["end"])
            out.pop(i)
            merged = any(len(x["_vals"]) < min_len for x in out) and len(out) > 1
            break

    labels = {"positive": "正相关", "negative": "负相关", "neutral": "零相关"}
    phases = []
    for seg in out:
        vals = seg.pop("_vals")
        mean = float(np.mean(vals))
        # 相位符号以该段均值定：吸收短段后若均值翻号，标签必须跟着变，
        # 否则会出现「label=正相关、mean_corr=-0.2」的自相矛盾。
        sign = "positive" if mean > 0 else ("negative" if mean < 0 else "neutral")
        phases.append({"start": seg["start"], "end": seg["end"],
                       "sign": sign, "label": labels[sign],
                       "mean_corr": _round(mean, 3), "n": len(vals)})
    phases.sort(key=lambda x: x["start"])
    return phases


def quantile_layers(x, y, unit: str = "", discrete: bool = False, current_value=None):
    """五分位分层未来收益（离散因子按唯一档位分桶）。

    返回 `(layers, current_index, current_label)`；`current_index` 是 1 基桶号
    （与契约的 `bucket_index` 一致），当前值不落在任何桶时为 None。
    """
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    m = np.isfinite(xs) & np.isfinite(ys)
    xs, ys = xs[m], ys[m]
    if len(xs) < 5:
        return [], None, None

    levels = None
    if discrete:
        levels = sorted({float(v) for v in xs})
        if len(levels) > 12:        # 离散因子档位过多 → 退回分位分桶
            levels = None

    bounds, masks, labels, codes = [], [], [], []
    if levels:
        for v in levels:
            bounds.append((v, v))
            masks.append(xs == v)
            labels.append("档位 %s%s" % (_fmt_num(v), unit))
            codes.append(_fmt_num(v))
    else:
        qs = np.quantile(xs, [0.2, 0.4, 0.6, 0.8])
        edges = [None] + [float(q) for q in qs] + [None]
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            bounds.append((lo, hi))
            if lo is None:
                masks.append(xs <= hi)
                labels.append("Q%d（≤ %s%s）" % (i + 1, _fmt_num(hi), unit))
            elif hi is None:
                masks.append(xs > lo)
                labels.append("Q%d（> %s%s）" % (i + 1, _fmt_num(lo), unit))
            else:
                masks.append((xs > lo) & (xs <= hi))
                labels.append("Q%d（%s ~ %s%s）" % (i + 1, _fmt_num(lo), _fmt_num(hi), unit))
            codes.append("Q%d" % (i + 1))

    # 当前读数落在哪个桶（离散按等值，连续按「下限开、上限闭」与分桶一致）
    cur_idx, cur_label = None, None
    if current_value is not None and _finite(current_value):
        cv = float(current_value)
        for i, (lo, hi) in enumerate(bounds):
            if levels:
                hit = cv == lo
            else:
                hit = (lo is None or cv > lo) and (hi is None or cv <= hi)
            if hit:
                cur_idx, cur_label = i + 1, labels[i]
                break

    layers = []
    for i, ((lo, hi), mk) in enumerate(zip(bounds, masks), start=1):
        yy = ys[mk]
        layers.append({
            "bucket": i,
            "code": codes[i - 1],
            "label": labels[i - 1],
            "lo": _round(lo, 4) if lo is not None else None,
            "hi": _round(hi, 4) if hi is not None else None,
            "mean_return": _round(float(np.mean(yy)), 2) if len(yy) else None,
            "median_return": _round(float(np.median(yy)), 2) if len(yy) else None,
            "win_rate": _round(float(np.mean(yy > 0)) * 100.0, 1) if len(yy) else None,
            "n": int(len(yy)),
            "current": bool(cur_idx == i),
        })
    return layers, cur_idx, cur_label


def current_reading(values, unit: str = "", lag: int = 20):
    """当前读数在自身历史里的位置：分位、区间、较 N 期前的变化。"""
    xs = [float(v) for v in (values or []) if _finite(v)]
    if not xs:
        return None
    cur = xs[-1]
    k = sum(1 for v in xs if v <= cur)
    q = np.quantile(xs, [0.25, 0.5, 0.75])
    change, change_label = None, "—"
    if len(xs) > lag:
        change = cur - xs[-(lag + 1)]
        change_label = "较 %d 期前 %+.4g%s" % (lag, change, unit)
    return {
        "value": _round(cur, 4),
        "percentile": _round(k / len(xs), 3),
        "min": _round(min(xs), 4),
        "p25": _round(float(q[0]), 4),
        "median": _round(float(q[1]), 4),
        "p75": _round(float(q[2]), 4),
        "max": _round(max(xs), 4),
        "change": _round(change, 4),
        "change_label": change_label,
        "n": len(xs),
    }


def _thin_indices(n: int, max_points: int) -> list[int]:
    """抽稀到 ≤max_points 个下标（保留首末），只用于控制 JSON 体积。"""
    if n <= 0:
        return []
    if max_points <= 0 or n <= max_points:
        return list(range(n))
    step = max(1, math.ceil(n / max_points))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    if len(idx) > max_points:
        idx = idx[:max_points - 1] + [n - 1]
    return idx


def _dual_series(fdates, fvals, tgt_dates, closes, max_points: int = MAX_POINTS):
    """因子 vs 标的的双轴走势（**标准化**：100 ± 10×标准差）。

    为什么不用 `value/first*100`：因子读数**可能是负数**（如红利④相对收益 -14pp），
    除以负的基期会让符号整体翻转，曲线跳到 -150~200，与目标价完全没法对比。
    改用 z-score（按各自的均值/标准差标准化）再平移到 100、每 1σ 记 10 点：
    两条线都以 100 为中线、幅度可比，且与正负号无关。标准差为 0 时退化为 100。
    `method`/`label` 一并给出，前端标题据此写「标准化」，不能再写「归一化到 100」。
    """
    tgt = list(tgt_dates or [])
    px = _price_array(closes)
    idx = _thin_indices(len(fdates), max_points)
    dates, fac, tar = [], [], []
    for i in idx:
        a = fvals[i] if i < len(fvals) else np.nan
        if not _finite(a):
            continue
        j = bisect.bisect_right(tgt, _to_date_str(fdates[i])) - 1
        if j < 0 or j >= len(px):
            continue
        b = px[j]
        if not _finite(b):
            continue
        dates.append(fdates[i])
        fac.append(float(a))
        tar.append(float(b))

    def _norm(values):
        arr = np.asarray(values, dtype=float)
        if not len(arr):
            return []
        sd = float(np.std(arr))
        if sd <= 1e-12:                 # 无波动 → 一条 100 的直线（不编形状）
            return [100.0] * len(arr)
        mu = float(np.mean(arr))
        return [_round(100.0 + (v - mu) / sd * 10.0, 2) for v in arr]

    return {
        "dates": dates,
        "factor": fac,
        "target": tar,
        "factor_norm": _norm(fac),
        "target_norm": _norm(tar),
        "method": "zscore_x10_base100",
        "label": "标准化（100 ± 10×标准差，看形状不看绝对刻度）",
    }


# ── 方向校验 ──────────────────────────────────────────────────────────

def is_discrete(spec) -> bool:
    spec = spec or {}
    if spec.get("discrete"):
        return True
    if spec.get("compute") in _DISCRETE_COMPUTES:
        return True
    return not spec.get("thresholds_key")


def _looks_discrete(values) -> bool:
    """读数只有寥寥几个整数 → 档位因子（阈值表往往不适用）。"""
    vs = {round(float(v), 6) for v in (values or []) if _finite(v)}
    if not vs or len(vs) > 5:
        return False
    return all(abs(v - round(v)) < 1e-9 for v in vs)


def expected_sign_of(spec, thresholds: dict, discrete: bool | None = None) -> str:
    """从阈值表**单调性**推断「读数越高是不是越好」。

    表是定义侧唯一的打分事实源：按读数区间从小到大看分数走势，
    单调升 → positive（读数越高越有利）、单调降 → negative、其它 → mixed。
    离散档位因子返回 discrete（读数即分数，不存在两套方向）。
    """
    if discrete is None:
        discrete = is_discrete(spec)
    if discrete:
        return "discrete"
    key = (spec or {}).get("thresholds_key")
    table = (thresholds or {}).get(key) if key else None
    rows = [r for r in (table or []) if isinstance(r, list) and len(r) == 3]
    if len(rows) < 2:
        return "mixed"
    rows = sorted(rows, key=lambda r: r[0])
    scores = [r[2] for r in rows]
    inc = all(b >= a for a, b in zip(scores, scores[1:]))
    dec = all(b <= a for a, b in zip(scores, scores[1:]))
    if inc and scores[-1] > scores[0]:
        return "positive"
    if dec and scores[-1] < scores[0]:
        return "negative"
    return "mixed"


def _sign(corr, weak: float = _WEAK_CORR) -> str:
    if corr is None or not _finite(corr):
        return "weak"
    f = float(corr)
    if f >= weak:
        return "positive"
    if f <= -weak:
        return "negative"
    return "weak"


def direction_check(spec, thresholds: dict, corr_value, corr_score,
                    hlabel: str, discrete: bool) -> dict:
    """方向校验：阈值表隐含的方向 vs 实际收益方向，冲突必须显式告警。

    判定要点：`corr_value` 是「读数 vs 未来收益」，`corr_score` 是「评分 vs
    未来收益」。阈值表单调升时两者同号；一旦 `corr_score` 显著为负，说明
    **评分表方向可能反了**（高读数被记正分，但历史高读数之后收益更低），
    这种自相矛盾比「相关弱」严重得多。
    """
    expected = expected_sign_of(spec, thresholds, discrete)
    value_sign = _sign(corr_value)
    score_sign = _sign(corr_score)
    # 只有「读数与收益的相关本身不弱」才判方向：|r|<0.15 时 aligned 一律 None。
    # 否则 hog rank r=-0.08 这类噪声会被报成「评分方向可能反了」，比不判更误导。
    aligned = None
    if value_sign != "weak":
        if discrete or expected == "mixed":
            if score_sign != "weak":
                aligned = (score_sign == "positive")
        else:
            aligned = (value_sign == expected)

    fmt = lambda v: "—" if v is None or not _finite(v) else "%+.2f" % float(v)
    if discrete:
        text = ("离散档位因子：读数即分数（与%s收益 r=%s）；%s"
                % (hlabel, fmt(corr_score),
                   {"positive": "评分越高、后续收益越高",
                    "negative": "评分越高、后续收益反而越低",
                    "weak": "相关性弱，方向不可判"}.get(score_sign, "方向不可判")))
    elif expected == "mixed":
        text = ("阈值表非单调，无法从表结构推断方向；评分与%s收益相关 r=%s"
                % (hlabel, fmt(corr_score)))
    elif aligned is True:
        text = ("读数越高，%s收益%s（r=%s）；评分表方向与收益一致。"
                % (hlabel, "越高" if expected == "positive" else "越低", fmt(corr_value)))
    elif aligned is False:
        text = ("⚠ 评分方向可能与收益相反：读数与%s收益 r=%s，评分相关 r=%s"
                "——阈值表方向需复核，勿据此加权。" % (hlabel, fmt(corr_value), fmt(corr_score)))
    else:
        text = "相关性弱（|r|<%.2f），方向校验不适用。" % _WEAK_CORR

    if (corr_score is not None and _finite(corr_score)
            and float(corr_score) <= -_WEAK_CORR
            and aligned is not False and _sign(corr_value) != "weak"):
        text += (" ⚠ 评分与未来收益显著负相关（corr_score=%s），评分方向可能反了。"
                 % fmt(corr_score))

    return {"expected_sign": expected, "aligned": aligned,
            "score_sign": score_sign, "text": text}


# ── 单因子证据组装 ────────────────────────────────────────────────────

def _horizon_stats(values, scores, futures, h: int, hlabel: str, unit: str,
                   discrete: bool, current_value):
    """单个期限的完整统计：corr_value / corr_score / 分层 / Q5−Q1。

    三个输入数组**等长**、且都以因子观测日为轴（见 `forward_returns_at`）：
    样本数 n 就是「有因子读数且有未来收益的观测数」，不再是目标日数。
    """
    fv = np.asarray(values, dtype=float)
    fs = np.asarray([np.nan if s is None else s for s in scores], dtype=float)
    fut = np.asarray(futures, dtype=float)
    mask = np.isfinite(fv) & np.isfinite(fut)
    x, y = fv[mask], fut[mask]
    smask = np.isfinite(fs) & np.isfinite(fut)
    xs, ys = fs[smask], fut[smask]

    item = {"h": h, "label": hlabel, "n": int(len(x)),
            "corr_value": None, "corr": None, "corr_score": None,
            "spearman_value": None, "spearman_score": None,
            "q5_q1": None, "layers": []}
    if len(x) >= MIN_HORIZON_SAMPLES:
        item["corr_value"] = item["corr"] = _round(pearson(x, y), 3)
        item["spearman_value"] = _round(spearman(x, y), 3)
    else:
        item["note"] = "样本不足（%d < %d）" % (len(x), MIN_HORIZON_SAMPLES)
    if len(xs) >= MIN_HORIZON_SAMPLES:
        item["corr_score"] = _round(pearson(xs, ys), 3)
        item["spearman_score"] = _round(spearman(xs, ys), 3)

    layers, cur_idx, cur_label = quantile_layers(x, y, unit, discrete, current_value)
    item["layers"] = layers
    if len(layers) >= 2:
        lo, hi = layers[0].get("mean_return"), layers[-1].get("mean_return")
        if lo is not None and hi is not None:
            # n < 30 时**不给 Q5−Q1 结论**：分层差在 20 来个样本上可以任意大，
            # 留在 JSON 里一定会被当成「显著结论」展示（分层明细照留，各桶自带 n）。
            if len(x) >= MIN_HORIZON_SAMPLES:
                item["q5_q1"] = round(hi - lo, 2)
    return item, {"x": x, "y": y, "xs": xs, "ys": ys,
                  "cur_index": cur_idx, "cur_label": cur_label}


def _indep_count(n, h) -> int:
    """把「重叠日频期数」换算成「独立窗口数」（n/h），无意义时返回 0。

    为什么需要：接口给出的 n 是**每个交易日一个前瞻窗口**，相邻窗口共享 h-1 天，
    有效独立周期只有 n/h 个，显著性因此被高估约 √h 倍。不点破这一点，读表的人
    会把 n=270 当成 270 个独立观测。

    2026-11-01 实测（a_share_regime ① 汇率，2016-2026）：
      重叠口径  r=+0.43 (n=270)
      每 20 日取一点 r=+0.157 (n=145，5% 临界 0.16) → 不显著
      每 60 日取一点 r=+0.251 (n=48，临界 0.29)  → 不显著
    接口的「评分方向可能与收益相反」警告正是因此不能直接当作「阈值写反了」。
    """
    try:
        n_i, h_i = int(n or 0), int(h or 0)
    except (TypeError, ValueError):
        return 0
    if n_i <= 0 or h_i <= 1:
        return 0
    return max(n_i // h_i, 1)


def _verdict_text(primary: dict, current: dict, direction: dict) -> str:
    """把统计量翻译成人话结论（与 `_evidence_verdict` 的分档口径保持一致）。"""
    c = primary.get("corr_value")
    if c is None:
        n = int(primary.get("n") or 0)
        need = max(0, MIN_HORIZON_SAMPLES - n)
        return ("样本不足（n=%d，至少需 %d 期才能给相关性结论；还需积累约 %d 期），"
                "此处只给当前读数与历史分位"
                % (n, MIN_HORIZON_SAMPLES, need))
    sign = "正" if c > 0 else "负"
    if primary.get("short_window"):
        # 短窗口是为了「别留一片空白」，不是为了让样本不足的因子显得可信：
        # 收益重叠更严重、r 天生更飘，一律按「方向参考」表述，不给区分度结论。
        text = ("短窗口（%s）下读数与收益%s相关（r=%+.2f，n=%d）；常规期限样本仍不足，"
                "仅作方向参考，尚不能据此确认区分度"
                % (primary["label"], sign, c, primary["n"]))
        if current.get("percentile") is not None:
            text += "；当前读数处于历史 %.0f%% 分位" % (current["percentile"] * 100.0)
        return text
    head = "因子读数与%s呈%s相关（r=%+.2f" % (primary["label"], sign, c)
    if primary.get("spearman_value") is not None:
        head += "，Spearman %+.2f" % primary["spearman_value"]
    # n 后面紧跟独立窗口数：读者第一眼看到的就是 n，不在这里点破就会被当成独立样本量
    _indep = _indep_count(primary.get("n"), primary.get("h"))
    head += "，n=%d%s）" % (primary["n"],
                            ("，独立窗口约 %d 个" % _indep) if _indep else "")
    parts = [head]
    q = primary.get("q5_q1")
    if q is not None:
        parts.append("按读数分五组后最高组比最低组 %+.2fpp" % q)
    if current.get("percentile") is not None:
        parts.append("当前读数处于历史 %.0f%% 分位" % (current["percentile"] * 100.0))
    text = "；".join(parts) + "。"

    ac, aq = abs(c), abs(q or 0)
    if ac >= 0.3 and aq >= 2:
        text += "该因子有实际区分度。"
    elif ac >= 0.15:
        text += "强度中等，可作辅助依据。"
    else:
        text += "暂无实际区分度，判断时请勿依赖。"
    if direction.get("aligned") is False:
        tip = str(direction.get("text") or "")
        text += tip if tip.startswith("⚠") else "⚠ " + tip
    return text


def build_evidence(defn, spec, ctx=None, engine=None, params=None, target=None,
                   snapshot_rows=None, max_points: int = MAX_POINTS,
                   max_replay_points: int = MAX_REPLAY_POINTS) -> dict:
    """单因子证据（契约 5.2）。

    `target` 为 `resolve_target()` 的结果；`snapshot_rows` 为
    `snapshot_series()` 的结果（手工因子用）。所有统计都在这里完成，
    `server/thesis_service` 只负责准备 ctx / 缓存 / 批量编排。
    """
    defn = defn or {}
    spec = spec or {}
    params = params or {}
    ctx = ctx or {}
    key = spec.get("key") or ""
    unit = spec.get("unit") or ""
    base = {"factor_key": key, "factor_name": spec.get("name") or key, "unit": unit}

    compute = spec.get("compute")
    source = "snapshot" if compute == "manual" else "replay"
    rows: list[dict] = []
    if source == "replay":
        # ⚠ 这里必须用**重放上限**而不是输出抽稀上限：把 240 传进来会让候选日
        # 多于 240 的因子被隔点采样（premium 只剩 26 个点 → 误判「样本不足」）。
        rows = replay_factor_series(engine, spec, ctx, defn, params,
                                    (target or {}).get("dates") or [],
                                    max_replay_points)
        # 自动口径一个点都回放不出来、但快照已在累积 → 降级到快照口径（明说来源）。
        # 只是「回放样本少」不切换：两套样本混用会让「样本不足」的期数失真。
        if not rows and snapshot_rows:
            rows, source = list(snapshot_rows), "snapshot"
    else:
        rows = list(snapshot_rows or [])
    base["source"] = source

    if not rows:
        if compute == "manual":
            reason = "手工维护因子，暂无自动时序（快照尚无该因子读数）"
        elif target is None:
            reason = "未配置目标标的（定义缺 evidence.target，且无回退标的/指数码）"
        else:
            reason = "该因子无可回放的历史序列（数据缺失或 compute 未注册）"
        base.update({"supported": False, "reason": reason, "series_start": ""})
        return base

    values = [r["value"] for r in rows]
    discrete = is_discrete(spec) or _looks_discrete(values)
    base["discrete"] = discrete
    base["series_start"] = rows[0]["date"]
    base["sample"] = {"start": rows[0]["date"], "end": rows[-1]["date"], "n": len(rows)}

    freq = (target or {}).get("freq") or "daily"
    meta = freq_meta(freq)
    # 当前读数的「较 N 期前」按**因子观测**频率取（月频因子的一期就是一个月）
    fmeta = freq_meta(detect_freq([r["date"] for r in rows]))
    cur = current_reading(values, unit, fmeta["lag"]) or {}

    if target is None:
        base.update({"supported": False,
                     "reason": "未配置目标标的（定义缺 evidence.target，且无回退标的/指数码）"})
        if cur:
            base["current"] = cur
        return base

    if len(rows) < MIN_SAMPLES:
        if compute == "manual":
            reason = ("手工维护因子，暂无自动时序；快照样本 %d 期 < %d"
                      % (len(rows), MIN_SAMPLES))
        elif source == "snapshot":
            reason = ("自动时序不可回放；快照样本 %d 期 < %d（积累中）"
                      % (len(rows), MIN_SAMPLES))
        else:
            reason = "因子序列样本不足（%d 期 < %d）" % (len(rows), MIN_SAMPLES)
        base.update({"supported": False, "reason": reason, "verdict": reason})
        if cur:
            base["current"] = cur
        return base

    tgt_dates = target.get("dates") or []
    closes = [float(x) for x in (target.get("values") or [])]
    fdates = [r["date"] for r in rows]
    fvals = [float(r["value"]) for r in rows]
    fscores = [r.get("score") for r in rows]
    fv = np.asarray(fvals, dtype=float)
    fs = np.asarray([np.nan if s is None else float(s) for s in fscores], dtype=float)

    # horizons 的 h 按**目标标的**频率取（未来收益 = 目标价的 h 个观测）
    horizon_specs = list(meta["horizons"])
    futures = {h: forward_returns_at(fdates, tgt_dates, closes, h)
               for h, _ in horizon_specs}
    horizons: list[dict] = []
    arrays_by_h: dict = {}

    def _add_horizon(h, hlabel, short_window=False):
        item, arrays = _horizon_stats(fvals, fscores, futures[h], h, hlabel,
                                      unit, discrete, fvals[-1])
        if short_window:
            item["short_window"] = True
        horizons.append(item)
        arrays_by_h[h] = arrays

    for h, hlabel in horizon_specs:
        _add_horizon(h, hlabel)

    # 常规期限（20/60 或 4/12…）都没攒够样本时补一个短窗口期限。
    # 前置条件：短窗口尚未在期限内（月频的 1 个月已经是它最短的常规期限）。
    short = _SHORT_HORIZON.get(freq)
    if short and all((it.get("n") or 0) < MIN_HORIZON_SAMPLES for it in horizons):
        h_short, label_short = short
        if h_short not in futures:
            futures[h_short] = forward_returns_at(fdates, tgt_dates, closes, h_short)
        _add_horizon(h_short, label_short, short_window=True)

    # 代表期限 = 首个「样本足够且有相关系数」的期限；都不够时退回首个期限。
    prim = next((it for it in horizons
                 if (it.get("n") or 0) >= MIN_HORIZON_SAMPLES
                 and it.get("corr_value") is not None), None) or horizons[0]
    prim_arrays = arrays_by_h[prim["h"]]

    # 当前桶（用首个期限的分桶，页面「当前桶历史平均收益」读的就是它）
    if prim_arrays and prim_arrays.get("cur_index"):
        idx = prim_arrays["cur_index"]
        layer = next((l for l in prim.get("layers") or [] if l["bucket"] == idx), None)
        cur["bucket_index"] = idx
        cur["bucket_label"] = prim_arrays.get("cur_label")
        cur["bucket_code"] = (layer or {}).get("code")
        if layer:
            cur["bucket_mean_return"] = layer.get("mean_return")
            cur["bucket_win_rate"] = layer.get("win_rate")
            cur["bucket_n"] = layer.get("n")

    # 滚动相关：序列轴是因子观测日（避免 ffill 伪样本），但**窗口长度按目标频率**
    # （日 60 / 周 12 / 月 6）。不能用 detect_freq(因子日期)：稀疏或长序列被
    # MAX_REPLAY_POINTS 采样后，采样间隔（如 4 天一个点）会被误判成「周频」，
    # 窗口缩到 12 期 → 曲线剧烈抖动。
    h_primary = prim["h"]
    fut_primary = futures[h_primary]
    # sample 的「总数 n」与「有效 n」是两件事：因子观测有 225 期，但目标日线只覆盖
    # 近 2 年时只有 25 期能算出未来收益。两个数都给出，避免把 225 当成统计样本量。
    eff_mask = np.isfinite(fv) & np.isfinite(fut_primary)
    eff_idx = np.where(eff_mask)[0]
    base["sample"].update({
        "effective_n": int(eff_mask.sum()),
        "effective_start": fdates[eff_idx[0]] if len(eff_idx) else None,
        "effective_end": fdates[eff_idx[-1]] if len(eff_idx) else None,
        "note": "n=因子观测总数；effective_n=有未来收益、真正进入统计的样本数",
    })
    win = max(3, min(meta["window"], max(10, len(rows) // 2)))
    rc = rolling_corr(fv, fut_primary, win)
    rcs = rolling_corr(fs, fut_primary, win)
    valid = [float(v) for v in rc if _finite(v)]
    pos = sum(1 for v in valid if v > 0)
    neg = sum(1 for v in valid if v < 0)
    idx = _thin_indices(len(fdates), max_points)
    last_val = next((float(rc[i]) for i in reversed(range(len(rc))) if _finite(rc[i])), None)
    last_score = next((float(rcs[i]) for i in reversed(range(len(rcs))) if _finite(rcs[i])), None)
    rolling = {
        "window": win, "h": h_primary,
        "label": "滚动 %d 期 · %s" % (win, prim["label"]),
        # 滚动窗口末端天然滞后 h 期（未来收益要等 h 期才能算出来），
        # 这不是 bug，必须在 label/caveats 里说清。
        "dates": [fdates[i] for i in idx],
        "corr_value": [_round(rc[i], 3) for i in idx],
        "corr_score": [_round(rcs[i], 3) for i in idx],
        "positive_share": _round(pos / len(valid) * 100.0, 1) if valid else None,
        "negative_share": _round(neg / len(valid) * 100.0, 1) if valid else None,
        "current_corr_value": _round(last_val, 3),
        "current_corr_score": _round(last_score, 3),
        # 长度 <3 期的一两期反转视为估计噪声，并入相邻段（见 corr_phases）
        "phases": corr_phases(fdates, rc, min_len=3),
    }

    scatter_idx = _thin_indices(len(prim_arrays["x"]), max_points)
    scatter = [[_round(prim_arrays["x"][i], 4), _round(prim_arrays["y"][i], 3)]
               for i in scatter_idx
               if _finite(prim_arrays["x"][i]) and _finite(prim_arrays["y"][i])]
    score_idx = _thin_indices(len(prim_arrays["xs"]), max_points)
    scatter_score = [[_round(prim_arrays["xs"][i], 4), _round(prim_arrays["ys"][i], 3)]
                     for i in score_idx
                     if _finite(prim_arrays["xs"][i]) and _finite(prim_arrays["ys"][i])]

    # 方向校验固定用**首个期限**（与滚动/散点/当前桶同一口径），
    # 只在 |corr_value| ≥ 0.15 时判 aligned——弱相关不该报「方向反了」。
    direction = direction_check(spec, defn.get("thresholds") or {},
                                prim.get("corr_value"), prim.get("corr_score"),
                                prim["label"], discrete)
    direction["h"] = prim.get("h")
    verdict = _verdict_text(prim, cur, direction)

    caveats = list(_CAVEATS)
    caveats.append("样本轴是因子观测日（%d 期），不是目标标的逐日；稀疏指标的有效样本更少。"
                   % len(rows))
    if prim.get("short_window"):
        caveats.insert(0, "常规期限（%s）样本不足，此处用「%s」短窗口：仅作方向参考，"
                          "收益重叠比常规期限更严重、显著性更弱。"
                       % ("/".join(h["label"] for h in horizons if not h.get("short_window")),
                          prim["label"]))
    caveats.append("未来 %d 期收益窗口重叠，相邻样本共享 %d 期，有效独立样本约 n/%d。"
                   % (h_primary, max(0, h_primary - 1), h_primary))
    caveats.append("滚动相关窗口 %d 期（按目标频率），末端因「未来收益需等 %d 期」天然滞后 %d 期。"
                   % (win, h_primary, h_primary))
    if source == "snapshot":
        caveats.insert(0, "序列来自每日快照累积（非重放），频率与数据日期以快照为准。")

    base.update({
        "supported": True,
        "target": {**{k: target.get(k) for k in
                      ("code", "name", "alias", "kind", "freq", "freq_label", "n")},
                   # 单位给「走势对照」的右轴用：双轴图必须标清两边各是什么量纲，
                   # 否则「Shbor 1.39%」和「债券指数 225」画在一起会被误读成同一尺度。
                   "unit": ((defn.get("evidence") or {}).get("target") or {}).get("unit") or ""},
        "current": cur,
        "horizons": horizons,
        "rolling": rolling,
        "scatter": scatter,
        "scatter_score": scatter_score,
        "dual": _dual_series(fdates, fvals, tgt_dates, closes, max_points),
        "direction": direction,
        "verdict": verdict,
        "caveats": caveats,
        "reason": None,
    })
    return base


# ── 变动归因（最新两个历史点） ────────────────────────────────────────

def _fmt_total(v) -> str:
    if v is None:
        return "—"
    try:
        return "%+d" % int(v)
    except (TypeError, ValueError):
        return "%+.2f" % float(v)


def _fmt_delta(v) -> str:
    if v is None:
        return "—"
    return "%+d" % int(v)


def _short_name(name, key) -> str:
    """「④ 成长风格相对强弱」→「④」：归因一句话里只要标号，名字太长了。"""
    n = str(name or "").strip()
    if not n:
        return str(key or "?")
    head = n.split(" ")[0]
    return head if len(head) <= 2 and head != n else n


def build_attribution(items, names=None) -> dict:
    """最新两个历史点的逐因子 Δ（回答「今天这一分是谁给的」）。

    只对比相邻两点：跨多点对比会把「中间已经反转的因子」平均掉，
    而这正是用户要看的变动来源。
    """
    items = list(items or [])
    names = names or {}
    if len(items) < 2:
        return {"prev_trade_date": None,
                "cur_trade_date": items[-1].get("trade_date") if items else None,
                "total_delta": None, "rows": [],
                "note": "历史点不足 2 个（现 %d 个），无法给出变动归因；至少再刷新一次。"
                        % len(items)}

    prev, cur = items[-2], items[-1]
    ps, cs = prev.get("scores") or {}, cur.get("scores") or {}
    pv, cv = prev.get("values") or {}, cur.get("values") or {}
    units = cur.get("units") or prev.get("units") or {}
    keys = list(dict.fromkeys(list(ps.keys()) + list(cs.keys())))

    rows = []
    for k in keys:
        a, b = ps.get(k), cs.get(k)
        delta = (b - a) if (a is not None and b is not None) else None
        va, vb = pv.get(k), cv.get(k)
        vdelta = round(vb - va, 4) if (va is not None and vb is not None) else None
        rows.append({
            "key": k,
            "name": names.get(k) or k,
            "prev_score": a, "cur_score": b, "delta": delta,
            "prev_value": va, "cur_value": vb, "value_delta": vdelta,
            "unit": units.get(k) or "",
            "direction": (None if delta is None else
                          ("up" if delta > 0 else ("down" if delta < 0 else "flat"))),
        })
    rows.sort(key=lambda r: (r["delta"] is None, -abs(r["delta"] or 0)))

    pt, ct = prev.get("total"), cur.get("total")
    total_delta = (ct - pt) if (pt is not None and ct is not None) else None
    moved = [r for r in rows if r["delta"]]
    if moved:
        detail = "、".join("%s %s" % (_short_name(r["name"], r["key"]), _fmt_delta(r["delta"]))
                          for r in moved[:6])
        unchanged = len(rows) - len(moved)
        note = ("总分 %s → %s（%s），其中 %s；其余 %d 项不变"
                % (_fmt_total(pt), _fmt_total(ct), _fmt_delta(total_delta), detail, unchanged))
    else:
        note = ("总分 %s → %s（%s），各因子分数无变化"
                % (_fmt_total(pt), _fmt_total(ct), _fmt_delta(total_delta)))

    return {"prev_trade_date": prev.get("trade_date"),
            "cur_trade_date": cur.get("trade_date"),
            "total_delta": total_delta, "rows": rows, "note": note}
