"""台账因子计算引擎：按定义取数 → 区间打分 → 综合结论 / 信号命中 / 失效条件。

**因子是定义驱动的，不是写死的**：台账定义（`data/thesis/*.json`）声明
`data`（要哪些序列/行情）与 `factors`（用哪些因子计算器、各自读什么），
引擎按注册表分发。新增第三个台账 = 一份 JSON + 必要时一个新因子计算器，
**不改引擎**。

取数原则
--------
- **能复用就复用**：宏观与利率走 `macro_db`（`macro_indicator` 长表），指数行情走
  `market_data_db.index_daily`；库内不足才触发采集，不自己造一套取数。
- **取不到就标缺失**：`score=None` + `status='数据缺失'`，不静默给 0 分——
  0 分代表「中性」，与「没数据」在决策上完全不同。
- **陈旧要显式**：手工项按 `stale_days` 判定，日频指标按 `macro_db.stale_threshold_days`
  判定（可在 data 声明里用 `stale_days` 覆盖），陈旧读数照常展示但会进 warnings。
- **「要不要补数」与「算不算陈旧」是两件事**（真实事故）：见 `_needs_refresh`。
  混用会导致数据落后一两天却不触发采集，页面刷新与定时刷新都只是拿旧数据重算。
"""
import logging
from datetime import date, datetime, timedelta

from thesis import defs as thesis_defs

logger = logging.getLogger(__name__)

# 抽取到的指标数不足时拥挤度因子的最低样本要求
_MIN_ROWS_FOR_CROWDING = 120

# 因子历史分位的最少样本数。低于此值只显示读数、不给分位：
# 20 个点的分位几乎没有统计意义，而「给了分位」会让读者以为它可信。
_MIN_SAMPLES_FOR_RANK = 30


class _SafeDict(dict):
    """模板里引用了手工项没有的字段时输出 —，而不是抛 KeyError。"""

    def __missing__(self, key):
        return "—"


# `dual_axis` 的 9 个方向组合 → 人话解释（键与定义 matrices.dual_axis 一一对应）。
# 三档方向（up/down/flat）两两组合，全部显式列出，便于定义侧按需调分。
_DUAL_AXIS_DESC = {
    "num_up_denom_down": "中国上行（复苏→盈利改善）+ 美债下行（美元流动性转松）→ 双利好",
    "num_up_denom_up": "中国复苏 + 美债上行 → 拉锯（分子端利多 vs 分母端利空）",
    "num_down_denom_down": "中国走弱 + 美债下行 → 拉锯（分母端利多 vs 分子端利空）",
    "num_down_denom_up": "中国走弱（分子端转差）+ 美债上行（美元流动性收紧）→ 双杀",
    "num_flat_denom_up": "中国利率基本未动 → 分子端无信号，由美债上行（收紧）单轴主导",
    "num_flat_denom_down": "中国利率基本未动 → 分子端无信号，由美债下行（转松）单轴主导",
    "num_up_denom_flat": "美债基本未动 → 分母端无信号，由中国复苏（分子端）单轴主导",
    "num_down_denom_flat": "美债基本未动 → 分母端无信号，由中国走弱（分子端）单轴主导",
    "num_flat_denom_flat": ("两轴均未动 → 无有效信号"
                            "（实证上美债高位维持期相关性会归零，"
                            "注意这是「没变化」而不是「没影响」）"),
}


class ThesisEngine:
    """台账计算引擎。取数方法刻意做成实例方法，便于测试整体替换。"""

    # ── 因子注册表：定义里的 compute 字段 → 计算方法（类体定义完后回填） ──
    FACTOR_COMPUTERS: dict = {}

    def __init__(self, logger_=None):
        self.logger = logger_ or logger

    # ── 取数层（测试覆盖这些方法即可完全离线） ──────────────────────────

    def today(self) -> date:
        return date.today()

    def macro_series(self, code: str, periods: int = 60) -> list[dict]:
        """宏观指标时序（升序）。**落后于「最近应有数据日」才去抓，抓不到就沿用库内。**

        真实事故（2026-09-18）：原实现是「库里只要有行就立刻返回」，于是第一次抓完
        就再也不更新——页面点刷新、bootstrap 定时刷新都只是拿同一批旧数据重算，
        `data_asof` 永远不动；而快照按**数据口径日期**归档，于是每次刷新都覆盖同一行，
        趋势图永远只有一个点。实测上游每天都有新数据（us10_y 库里停 9-16、上游已到 9-17），
        完全不是「源没更新」。
        """
        from utils.cache import macro_db
        rows = macro_db.get_macro_series(code, periods=periods)
        latest = rows[-1]["report_date"] if rows else None
        # 派生码（`idx_*`，如 idx_h30269_dy 股息率）不在 MACRO_INDICATOR_META 里，
        # 由 `sync_index_valuation` 单独维护；对它们调 fetch_and_store_macro
        # 只会得到「未知指标」。这里直接沿用库内数据，别做无意义的抓取。
        if code not in macro_db.MACRO_INDICATOR_META:
            return rows
        if rows and not self._needs_refresh(latest, self.macro_period_type(code)):
            return rows
        # 需要补数；抓取失败**不致命**，继续用库内已有的——绝不因为一次网络抖动把读数丢掉
        try:
            macro_db.fetch_and_store_macro([code])
        except Exception as e:
            self.logger.warning("[Thesis] 宏观指标 %s 拉取失败，沿用库内数据: %s", code, e)
            return rows
        fresh = macro_db.get_macro_series(code, periods=periods)
        return fresh or rows

    def macro_period_type(self, code: str) -> str:
        """指标的发布周期（daily / monthly / quarterly），用于判断「该不该去补数」。"""
        from utils.cache import macro_db
        meta = macro_db.MACRO_INDICATOR_META.get(code) or {}
        return meta.get("period_type", "daily")

    def macro_stale_threshold(self, code: str) -> int:
        from utils.cache import macro_db
        return macro_db.stale_threshold_days(code)

    # ── 「要不要补数」判定 ────────────────────────────────────────────
    #
    # 这两个概念**必须分开**，混用是本次事故的根因：
    #   ① `_is_stale(asof, stale_threshold_days)` —— 回答「这个读数旧到该不该提示用户」。
    #      日频阈值是 10 天，语义是「10 天前的国债收益率就别信了」。
    #   ② `_needs_refresh(asof, period_type)` —— 回答「要不要现在去抓一遍新数据」。
    #      日频应当是「落后于最近应有交易日就抓」，通常就是差 1 天。
    #   把 ① 当 ② 用 → 数据落后 1~5 天都不补，页面刷新看着"没反应"。

    @staticmethod
    def _expected_latest_data_date(today: date | None = None) -> date:
        """最近一个「应该有数据」的日期：按工作日近似，**不含节假日**。

        节假日的代价只是多一次抓取（`added=0`），无害；反过来漏抓会导致
        「明明有新数据却一直不更新」，那才是本次要修的问题。
        """
        d = today or date.today()
        while d.weekday() >= 5:  # 5=周六 6=周日
            d -= timedelta(days=1)
        return d

    def _needs_refresh(self, asof, period_type: str = "daily") -> bool:
        """库内读数是否落后到需要重新抓取。与 `_is_stale` 语义不同（见上方说明）。"""
        if not asof:
            return True
        try:
            d = datetime.strptime(str(asof)[:10], "%Y-%m-%d").date()
        except ValueError:
            return True
        if period_type == "daily":
            return d < self._expected_latest_data_date(self.today())
        # 月/季频：给一个发布滞后窗口，窗口内视为「已是最新」
        lag = 45 if period_type == "monthly" else 135
        return (self.today() - d).days > lag

    def upsert_series(self, code: str, meta: dict, df, source: str) -> int:
        from utils.cache import macro_db
        return macro_db.upsert_indicator_series(code, meta, df, source=source)

    def fetch_index_valuation(self, index_code: str):
        """中证指数公司估值时序（列 report_date/dy/pe...）。失败抛异常由调用方降级。"""
        from tools.fetcher import ak_index_valuation
        return ak_index_valuation(index_code)

    def read_index_daily(self, code: str, days: int):
        """读库内指数日线（DataFrame，含 trade_date/close）。"""
        from utils.cache.market_data_db import get_index_daily
        return get_index_daily(code)

    def collect_index_daily(self, code: str, days: int, name: str = "") -> int:
        from utils.cache.market_data_db import fetch_and_store_index_daily
        return fetch_and_store_index_daily(code, name=name, days=days)

    # ── 组合数据：把指数估值落到 macro_indicator 长表 ──────────────────

    def sync_index_valuation(self, index_code: str) -> dict:
        """把指数股息率/PE 增量落库，返回 {dy_code, pe_code, added}。

        该源只返回近 20 个交易日，长期分位只能逐期累积——所以每次运行都灌一次。
        """
        code = str(index_code).strip().upper()
        dy_code, pe_code = f"idx_{code.lower()}_dy", f"idx_{code.lower()}_pe"
        meta = {"unit": "%", "period_type": "daily"}
        result = {"dy_code": dy_code, "pe_code": pe_code, "dy_added": 0, "ok": False}
        try:
            df = self.fetch_index_valuation(code)
            if df is None or df.empty:
                return result
            dy_df = df.rename(columns={"dy": "value"})[["report_date", "value"]]
            result["dy_added"] = self.upsert_series(dy_code, meta, dy_df, "csindex")
            if "pe" in df.columns:
                pe_df = df.rename(columns={"pe": "value"})[["report_date", "value"]].dropna()
                self.upsert_series(pe_code, meta, pe_df, "csindex")
            result["ok"] = True
        except Exception as e:
            self.logger.warning("[Thesis] 指数估值同步失败(%s): %s", code, e)
        return result

    # ── 数据预取：定义的 data 块 → ctx ────────────────────────────────

    def _prepare(self, defn: dict, params: dict, warnings: list) -> dict:
        """按定义的 data 声明预取数据。

        kind=macro           → ctx["series"][alias] = [{report_date, value}]（升序）
        kind=index_valuation → 同步估值后取指定字段（dy/pe）的序列
        kind=index_daily     → ctx["frames"][alias] = DataFrame（trade_date/close）

        每个别同时给出 stale_days（陈旧阈值），供因子计算器判定读数是否滞后。
        单个数据源失败只记 warning，不阻断其它数据源。
        """
        ctx: dict = {"series": {}, "frames": {}, "stale_days": {}}
        for alias, spec in (defn.get("data") or {}).items():
            kind = spec.get("kind")
            try:
                if kind == "macro":
                    rows = self.macro_series(spec["code"],
                                             periods=int(spec.get("periods", 400)))
                    ctx["series"][alias] = rows
                    ctx["stale_days"][alias] = int(
                        spec.get("stale_days")
                        or self.macro_stale_threshold(spec["code"]))
                elif kind == "index_valuation":
                    sync = self.sync_index_valuation(spec["index_code"])
                    field = spec.get("field", "dy")
                    if not sync["ok"]:
                        warnings.append("指数估值同步失败（%s），相关因子可能缺失"
                                        % spec["index_code"])
                    code = sync["dy_code"] if field == "dy" else sync["pe_code"]
                    ctx["series"][alias] = self.macro_series(code, periods=4000)
                    ctx["stale_days"][alias] = int(spec.get("stale_days", 10))
                elif kind == "index_daily":
                    ctx["frames"][alias] = self._load_index(
                        spec["code"], int(spec.get("days", params.get("index_hist_days", 900))),
                        spec.get("name", ""), warnings, spec.get("label", alias), params)
                else:
                    warnings.append("台账定义 data.%s 的 kind='%s' 无法识别，已跳过"
                                    % (alias, kind))
            except Exception as e:
                self.logger.warning("[Thesis] 取数失败 %s(%s): %s", alias, kind, e)
                warnings.append("数据源 %s 取数失败：%s" % (alias, str(e)[:80]))
        return ctx

    # ── 主入口 ────────────────────────────────────────────────────────

    def compute(self, thesis_id: str, defn: dict | None = None) -> dict:
        definition_problems: list[str] = []
        if defn is None:
            defn, definition_problems = thesis_defs.load_def_checked(thesis_id)
            if defn is None:
                raise ValueError(f"台账定义不可用: {thesis_id} ({definition_problems})")
        params = defn.get("params") or {}
        thresholds = defn.get("thresholds") or {}
        # 定义本身的问题要进 warnings：阈值区间没兜住会让因子误报「数据缺失」，
        # 不显式点出来就会被当成取数失败去排查，方向完全错。
        warnings: list[str] = list(definition_problems)

        ctx = self._prepare(defn, params, warnings)

        factors: list[dict] = []
        metrics: dict = {}
        specs = defn.get("factors") or []
        if not specs:
            warnings.append("台账定义缺少 factors 清单，无法计算任何因子")
        for spec in specs:
            fn = self.FACTOR_COMPUTERS.get(spec.get("compute"))
            if fn is None:
                warnings.append("因子 %s 的 compute='%s' 未注册，已按数据缺失处理"
                                % (spec.get("key"), spec.get("compute")))
                factors.append(self._make_factor(
                    key=spec.get("key", "?"), name=spec.get("name", "?"),
                    value=None, unit=spec.get("unit", ""), score=None,
                    detail="因子计算器未注册（定义错误）", source="—", asof="—"))
                continue
            try:
                factor, updates = fn(self, spec, ctx, defn, params, thresholds, warnings)
            except Exception as e:
                self.logger.warning("[Thesis] 因子 %s 计算失败: %s", spec.get("key"), e)
                warnings.append("因子 %s 计算失败：%s" % (spec.get("key"), str(e)[:80]))
                factor = self._make_factor(
                    key=spec.get("key", "?"), name=spec.get("name", "?"),
                    value=None, unit=spec.get("unit", ""), score=None,
                    detail="计算失败（数据或定义异常）", source="—", asof="—")
                updates = {}
            factors.append(factor)
            metrics.update(updates or {})

        # ── 因子历史分位：统一补算，避免每个计算器各写一遍 ──
        # 只在样本 ≥ _MIN_SAMPLES_FOR_RANK 时给值，不足一律留 None 由前端显示
        # 「样本不足」——**不要**用估算或省略来假装有分位：研究报告反复强调
        # 「n 小则相关性/统计量只能看方向」，编一个分位比不给更糟。
        for factor, spec in zip(factors, specs):
            # 置信度元信息（样本量 / 主观设定程度）：定义里声明，原样透传给前端。
            # 研究报告反复自我设限（「n=5~8 不宜作精确效应量」「五因子评分含主观权重」），
            # 这个字段就是让页面上能显示「这条读数有多可信」。
            if spec.get("confidence"):
                factor["confidence"] = spec["confidence"]
            # 读数本身就是分位（如黄金的位置维度）时不该再算一次「读数在序列中的分位」——
            # 那既无意义，又会因为读数是 0~100 而把「分位」显示成另一个含义的数。
            # 置位后由前端跳过该条并显示「本条即分位」。
            if spec.get("no_series_rank"):
                factor["pct_rank"] = None
                factor["pct_rank_off"] = True
                continue
            alias = spec.get("rank_series") or spec.get("series")
            series = (ctx["series"].get(alias) or []) if alias else []
            vals = [r["value"] for r in series if r["value"] is not None]
            if (factor.get("value") is None
                    or len(vals) < _MIN_SAMPLES_FOR_RANK):
                factor["pct_rank"] = None
                continue
            factor["pct_rank"] = _pct_rank(vals, factor["value"])

        # 综合结论
        valid = [f for f in factors if f["score"] is not None]
        total = sum(f["score"] for f in valid)
        verdict, action = self._verdict(total, len(valid), defn.get("verdicts") or [])
        if not valid:
            verdict, action = "数据不足", "无法判断"

        # 信号与失效条件
        signals = self._eval_signals(defn.get("signals") or {}, metrics)
        invalidation = self._eval_invalidation(defn.get("invalidation") or {}, metrics)

        # 研究表达层：把报告里的条件化结论变成可判断结构
        # （情景推演 / 阶段定位 / 传导路径——三者都不参与打分，只做"当前处在哪一格"）
        asof = self._max_asof(factors)
        scenarios = self._eval_blocks(defn.get("scenarios") or [], metrics)
        transmission = self._eval_blocks(defn.get("transmission") or [], metrics)
        phases = self._eval_phases(defn.get("phases") or [], asof)
        stages = self._eval_stages(defn.get("stages") or {}, factors, warnings)

        return {
            "thesis_id": defn.get("id", thesis_id),
            "name": defn.get("name", thesis_id),
            "asset_label": defn.get("asset_label", ""),
            # 指数口径声明（价格 / 全收益）：报告反复警告同一指数两种口径年化差 4~6pp，
            # 台账必须把当前口径显式带出来，否则读者会把两条口径的数混着比。
            "caliber_note": defn.get("_说明_口径") or "",
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_asof": self._max_asof(factors),
            "factors": factors,
            "total": total,
            "valid_count": len(valid),
            "factor_count": len(factors),
            "verdict": verdict,
            "action": action,
            "signals": signals,
            "invalidation": invalidation,
            "scenarios": scenarios,
            "transmission": transmission,
            "phases": phases,
            "stages": stages,
            "metric_values": metrics,
            "warnings": warnings,
        }

    # ── 因子计算器 ────────────────────────────────────────────────────
    #
    # 统一签名：fn(engine, spec, ctx, defn, params, thresholds, warnings)
    #           -> (factor_dict, metrics_updates)

    def _factor_rate_premium(self, spec, ctx, defn, params, thresholds, warnings):
        """两个水平的差（如 股息率 − 10Y 国债）。

        spec: dy / rate（数据别名）、metric、unit、thresholds_key、source、note
        """
        dy_series = ctx["series"].get(spec["dy"]) or []
        rate_series = ctx["series"].get(spec["rate"]) or []
        dy = dy_series[-1]["value"] if dy_series else None
        rate = rate_series[-1]["value"] if rate_series else None
        dy_date = dy_series[-1]["report_date"] if dy_series else None
        rate_date = rate_series[-1]["report_date"] if rate_series else None
        premium = round(dy - rate, 4) if (dy is not None and rate is not None) else None
        stale = self._is_stale(dy_date, ctx["stale_days"].get(spec["dy"], 10))
        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=premium, unit=spec.get("unit", "pp"),
            score=thesis_defs.score_by_table(premium, thresholds[spec["thresholds_key"]]),
            detail=("股息率 %.2f%% − 10Y国债 %.4f%%" % (dy, rate))
            if premium is not None else "数据缺失",
            source=spec.get("source", "—"),
            asof="%s / %s" % (dy_date, rate_date),
            stale=stale, note=spec.get("note", ""),
        )
        return factor, {spec["metric"]: premium, spec["dy"]: dy, spec["rate"]: rate}

    def _factor_spread_bp(self, spec, ctx, defn, params, thresholds, warnings):
        """两个水平之差，可按 scale 换算单位（港美利差 = HIBOR − 美债，×100 转 bp）。

        与 `_factor_rate_premium` 的三点差别：① 展示文案由 `label_a`/`label_b` 提供
        （前者把「股息率 − 10Y国债」写死在 detail 里，复用会让港美利差显示出错误文字）；
        ② 单位可换算成 bp；③ 不做趋势修正——利差本身就是两个趋势量的差。

        spec: a / b（数据别名）、metric、unit、scale、label_a、label_b、thresholds_key
        """
        a_series = ctx["series"].get(spec["a"]) or []
        b_series = ctx["series"].get(spec["b"]) or []
        a = a_series[-1]["value"] if a_series else None
        b = b_series[-1]["value"] if b_series else None
        a_date = a_series[-1]["report_date"] if a_series else None
        b_date = b_series[-1]["report_date"] if b_series else None
        scale = float(spec.get("scale", 1.0))
        val = round((a - b) * scale, 2) if (a is not None and b is not None) else None
        # 两腿任一陈旧即视为陈旧（利差的可信度取决于较旧的那条腿）
        stale = (self._is_stale(a_date, ctx["stale_days"].get(spec["a"], 10))
                 or self._is_stale(b_date, ctx["stale_days"].get(spec["b"], 10)))
        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=val, unit=spec.get("unit", "bp"),
            score=thesis_defs.score_by_table(val, thresholds[spec["thresholds_key"]]),
            detail=("%s %+.2f − %s %+.2f = %+.2f%s"
                    % (spec.get("label_a", "A"), a * scale,
                       spec.get("label_b", "B"), b * scale, val, spec.get("unit", "bp")))
            if val is not None else "数据缺失",
            source=spec.get("source", "—"), asof="%s / %s" % (a_date, b_date),
            stale=stale, note=spec.get("note", ""),
        )
        return factor, {spec["metric"]: val, spec["a"]: a, spec["b"]: b}

    def _factor_rate_level_trend(self, spec, ctx, defn, params, thresholds, warnings):
        """单指标水平分 + 趋势修正（红利②、债基①票息垫子、债基③资金面共用）。

        spec: series（数据别名）、code（陈旧阈值用）、metric、unit、thresholds_key、
              source、higher_is_better（默认 True；利率类为 False——越低越有利）、
              trend_lookback_days / trend_move_bp（缺省回落到 params）
        """
        series = ctx["series"].get(spec["series"]) or []
        latest = series[-1]["value"] if series else None
        asof = series[-1]["report_date"] if series else None
        if latest is None:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}

        unit = spec.get("unit", "")
        score = thesis_defs.score_by_table(latest, thresholds[spec["thresholds_key"]])
        detail = ("水平 %.4f%%" % latest) if unit == "%" else ("水平 %s" % latest)

        higher_is_better = bool(spec.get("higher_is_better", True))
        n = int(spec.get("trend_lookback_days", params.get("trend_lookback_days", 20)))
        move = float(spec.get("trend_move_bp", params.get("trend_move_bp", 10)))
        if score is not None and len(series) >= n + 1:
            prev = series[-(n + 1)]["value"]
            chg_bp = round((latest - prev) * 100, 1)
            if chg_bp <= -move:
                score = min(thesis_defs.SCORE_MAX, score + 1)
                detail += "；近%d个交易日下行 %.1fbp（%s）" % (
                    n, abs(chg_bp), "利好" if higher_is_better else "宽松/利好")
            elif chg_bp >= move:
                score = max(thesis_defs.SCORE_MIN, score - 1)
                detail += "；近%d个交易日上行 %.1fbp（%s）" % (
                    n, chg_bp, "压制" if higher_is_better else "收敛/不利")
            else:
                detail += "；近%d个交易日变动 %.1fbp（平稳）" % (n, chg_bp)
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=latest, unit=unit,
            score=score, detail=detail, source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: latest}

    def _factor_level_trend_pct(self, spec, ctx, defn, params, thresholds, warnings):
        """绝对水平分 + **相对变动**趋势修正（汇率类）。

        与 `_factor_rate_level_trend` 的唯一差别是趋势口径：利率用 bp 表述
        （1bp = 0.01%），而汇率报价是「1 美元兑多少人民币」，沿用 bp 会得出
        「近 20 个交易日变动 50bp」这种没人这么说的读数，故改用相对变动百分比。
        spec: series、metric、unit、thresholds_key、higher_is_better、
              trend_lookback_days / trend_move_pct、down_label / up_label（趋势措辞）
        """
        series = ctx["series"].get(spec["series"]) or []
        latest = series[-1]["value"] if series else None
        asof = series[-1]["report_date"] if series else None
        if latest is None:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}

        score = thesis_defs.score_by_table(latest, thresholds[spec["thresholds_key"]])
        detail = "水平 %.4f" % latest
        n = int(spec.get("trend_lookback_days", params.get("trend_lookback_days", 20)))
        move_pct = float(spec.get("trend_move_pct", 0.5))
        down_label = spec.get("down_label", "下行")
        up_label = spec.get("up_label", "上行")
        if score is not None and len(series) >= n + 1:
            prev = series[-(n + 1)]["value"]
            if prev:
                chg = (latest / prev - 1) * 100
                if chg <= -move_pct:
                    score = min(thesis_defs.SCORE_MAX, score + 1)
                    detail += "；近%d日 %+.2f%%（%s）" % (n, chg, down_label)
                elif chg >= move_pct:
                    score = max(thesis_defs.SCORE_MIN, score - 1)
                    detail += "；近%d日 %+.2f%%（%s）" % (n, chg, up_label)
                else:
                    detail += "；近%d日 %+.2f%%（平稳）" % (n, chg)
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=latest, unit=spec.get("unit", ""),
            score=score, detail=detail, source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: latest}

    def _factor_trend_bp(self, spec, ctx, defn, params, thresholds, warnings):
        """近 N 个交易日的变动（bp）**直接作为读数**（美元趋势类）。

        为什么不复用 `rate_level_trend`：那是「水平分 + 趋势修正」双档结构，而这里
        没有可用的水平分——合成美元指数的绝对水平与官方 ICE DXY 有偏差（外管局中间价
        含政策因子），只有**变动幅度**可比，所以阈值直接作用在变动量上。

        spec: series、metric、unit、thresholds_key、lookback_days、scale（默认 100 → bp）
        """
        series = ctx["series"].get(spec["series"]) or []
        n = int(spec.get("lookback_days", params.get("trend_lookback_days", 20)))
        scale = float(spec.get("scale", 100.0))
        if len(series) < n + 1:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "bp"),
                score=None, detail="历史不足 %d 期" % (n + 1),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        cur, prev = series[-1]["value"], series[-(n + 1)]["value"]
        val = round((cur - prev) * scale, 1)
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=val, unit=spec.get("unit", "bp"),
            score=thesis_defs.score_by_table(val, thresholds[spec["thresholds_key"]]),
            detail="近 %d 个交易日 %+.1f%s（%.4f → %.4f）"
                   % (n, val, spec.get("unit", "bp"), prev, cur),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: val}

    def _factor_double_tightening(self, spec, ctx, defn, params, thresholds, warnings):
        """内外双紧判定：中债变动 × 美债变动（债基②）。

        以中债为主轴（债基的定价锚在国内），美债作次要轴：
          中债下行 + 美债上行 → +2（外紧内松）
          中债下行 + 其它     → +1（宽松）
          中债上行 + 其它     → -1（国内自身收紧）
          中债上行 + 美债上行 → -2（内外双紧，历史上债基真正亏钱的组合）
          双方都没动          →  0
        判定表放在定义的 matrices 里，阈值改动无需改代码。
        """
        cn_series = ctx["series"].get(spec["cn"]) or []
        us_series = ctx["series"].get(spec["us"]) or []
        n = int(spec.get("lookback_days", params.get("trend_lookback_days", 20)))
        move = float(spec.get("move_bp", params.get("trend_move_bp", 10)))
        cn_latest = cn_series[-1]["value"] if cn_series else None
        us_latest = us_series[-1]["value"] if us_series else None

        updates = {spec.get("cn_metric", "cn10_y"): cn_latest,
                   spec.get("us_metric", "us10_y"): us_latest,
                   spec.get("cn_chg_metric", "cn10_chg_bp"): None,
                   spec.get("us_chg_metric", "us10_chg_bp"): None,
                   spec["metric"]: None}

        if cn_latest is None or us_latest is None or len(cn_series) < n + 1 \
                or len(us_series) < n + 1:
            factor = self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失或历史不足 %d 期" % (n + 1),
                source=spec.get("source", "—"), asof="—")
            return factor, updates

        cn_chg = round((cn_latest - cn_series[-(n + 1)]["value"]) * 100, 1)
        us_chg = round((us_latest - us_series[-(n + 1)]["value"]) * 100, 1)
        updates[spec.get("cn_chg_metric", "cn10_chg_bp")] = cn_chg
        updates[spec.get("us_chg_metric", "us10_chg_bp")] = us_chg

        matrix = (defn.get("matrices") or {}).get(spec["matrix"]) or {}
        cn_dir = "down" if cn_chg <= -move else ("up" if cn_chg >= move else "flat")
        us_dir = "up" if us_chg >= move else ("down" if us_chg <= -move else "flat")
        if cn_dir == "flat":
            # 中债没动 → 冲击有限（无论美债怎么走）。这是长周期复盘的核心结论：
            # 中国债市定价权在国内，美债上行只是情绪扰动，不该判成「内外双紧」。
            score = matrix.get("flat", 0)
            desc = ("中债基本未动、美债上行（情绪扰动为主，实质冲击有限）"
                    if us_dir == "up" else "中债基本未动（冲击有限）")
        elif cn_dir == "down":
            if us_dir == "up":
                score = matrix.get("cn_down_us_up", 2)
                desc = "中债下行 + 美债上行（外紧内松，利好债基）"
            else:
                score = matrix.get("cn_down_us_down", 1)
                desc = "中债下行（国内宽松，利好债基）"
        else:
            if us_dir == "up":
                score = matrix.get("cn_up_us_up", -2)
                desc = "中债上行 + 美债上行（内外双紧，历史上债基真正亏钱的组合）"
            else:
                score = matrix.get("cn_up_us_down", -1)
                desc = "中债上行（国内自身收紧，需警惕）"

        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=score, unit=spec.get("unit", ""),
            score=score,
            detail="近%d个交易日：中债 %+0.1fbp、美债 %+0.1fbp；判定：%s"
                   % (n, cn_chg, us_chg, desc),
            source=spec.get("source", "—"), asof=cn_series[-1]["report_date"],
            stale=self._is_stale(cn_series[-1]["report_date"],
                                 ctx["stale_days"].get(spec["cn"], 10)),
            note=spec.get("note", ""),
        )
        updates[spec["metric"]] = score
        return factor, updates

    def _factor_dual_axis(self, spec, ctx, defn, params, thresholds, warnings):
        """分子端（中国）× 分母端（美债）的**对称**双轴判定（恒生科技②）。

        与 `double_tightening` 的关键区别：那个以中债为主轴——债基定价锚在国内，
        所以「中债不动 = 冲击有限」是它的核心特例；本计算器**两轴对称**，没有任何
        一轴能单独定调，必须看方向组合。硬套前者会把恒生科技错判成「中债平稳=没事」，
        而中债平稳对恒生科技只是「分子端没信号」，分母端照样能定调。

        机制依据（74 个月实证，`data/thesis/hang_seng_tech.json` 头部有完整出处）：
          分子端看中国 —— 中国 10Y 变化 r=+0.28（加息期 +0.49），利率上行=复苏预期=盈利改善
          分母端看美债 —— 美债 10Y 变化 r=−0.28（加息期 −0.45），利率上行=美元流动性收紧
        两轴方向相反且都有量级，四象限判定表放定义的 matrices，改阈值无需改代码。

        spec: num / denom（数据别名）、matrix、metric、*_metric、*_chg_metric、
              lookback_days、move_bp、unit、source、note
        """
        num_series = ctx["series"].get(spec["num"]) or []
        denom_series = ctx["series"].get(spec["denom"]) or []
        n = int(spec.get("lookback_days", params.get("trend_lookback_days", 20)))
        move = float(spec.get("move_bp", params.get("trend_move_bp", 10)))

        num_metric = spec.get("num_metric", "num")
        denom_metric = spec.get("denom_metric", "denom")
        num_chg_metric = spec.get("num_chg_metric", "num_chg_bp")
        denom_chg_metric = spec.get("denom_chg_metric", "denom_chg_bp")

        num_latest = num_series[-1]["value"] if num_series else None
        denom_latest = denom_series[-1]["value"] if denom_series else None
        updates = {num_metric: num_latest, denom_metric: denom_latest,
                   num_chg_metric: None, denom_chg_metric: None, spec["metric"]: None}

        if num_latest is None or denom_latest is None \
                or len(num_series) < n + 1 or len(denom_series) < n + 1:
            factor = self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失或历史不足 %d 期" % (n + 1),
                source=spec.get("source", "—"), asof="—")
            return factor, updates

        num_chg = round((num_latest - num_series[-(n + 1)]["value"]) * 100, 1)
        denom_chg = round((denom_latest - denom_series[-(n + 1)]["value"]) * 100, 1)
        updates[num_chg_metric] = num_chg
        updates[denom_chg_metric] = denom_chg

        matrix = (defn.get("matrices") or {}).get(spec["matrix"]) or {}
        # 分子端：中国利率**上行**才是有利方向（复苏→盈利改善），与债基/红利相反
        num_dir = "up" if num_chg >= move else ("down" if num_chg <= -move else "flat")
        # 分母端：美债**下行**才是有利方向（美元流动性转松）
        denom_dir = "down" if denom_chg <= -move else ("up" if denom_chg >= move else "flat")

        # 统一按 num_<dir>_denom_<dir> 取 9 个方向组合（3×3），全部由定义的 matrices 决定。
        # 不写成「某轴 flat 就一律 0」——那会把另一轴的真实信号丢掉：
        # 中国没动而美债收紧，分母端该照常计分（只是强度减半），不该归零。
        key = "num_%s_denom_%s" % (num_dir, denom_dir)
        if key in matrix:
            score = matrix[key]
            desc = _DUAL_AXIS_DESC.get(key, "方向组合已计分")
        else:
            # 矩阵缺键：按中性处理，但**绝不能**照常输出一句「双杀/双利好」——
            # 那会让人话解释与评分自相矛盾（实测踩过：detail 写「双杀」而 score=0）。
            score = 0
            desc = "该方向组合（%s）未在 matrices.%s 中声明，已按中性处理" % (
                key, spec.get("matrix", ""))
            warnings.append("双轴矩阵缺 %s（已按中性处理），请检查定义 matrices.%s"
                            % (key, spec.get("matrix", "")))

        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=score, unit=spec.get("unit", ""),
            score=score,
            detail="近%d个交易日：中国 10Y %+0.1fbp、美债 10Y %+0.1fbp；判定：%s"
                   % (n, num_chg, denom_chg, desc),
            source=spec.get("source", "—"), asof=num_series[-1]["report_date"],
            stale=(self._is_stale(num_series[-1]["report_date"],
                                  ctx["stale_days"].get(spec["num"], 10))
                   or self._is_stale(denom_series[-1]["report_date"],
                                     ctx["stale_days"].get(spec["denom"], 10))),
            note=spec.get("note", ""),
        )
        updates[spec["metric"]] = score
        return factor, updates

    def _factor_manual(self, spec, ctx, defn, params, thresholds, warnings):
        """手工项（无自动数据源的因子）：险资欠配、汇率、赎回压力、政府债供给。

        spec: manual（manual 块的键）、value_field、metric、stale_metric、
              unit、thresholds_key、detail_template（缺字段输出 —）、source、
              derive（可选：从 item 派生出真正被打分的读数，见下）

        **derive（2026-09-28 新增，为猪周期能繁母猪而加）**：有些手工项的「读数」
        与「该被打分的东西」不是同一个量——能繁存栏 3780 万头本身没有好坏，有意义的是
        「较本轮高点 4080 去化了多少（7.35%）」。derive 是一个小表达式，取值形如：

            {"op": "drop_pct", "from": "peak", "of": "value"}

        即 (1 − value/peak) × 100，产出的键写进 spec["metrics"] 声明的指标名
        （本例 sow_dev_pct），阈值与信号都作用在派生量上；原始读数仍以 spec["metric"]
        落库并展示在 detail 里。**不要为了一个台账去改阈值语义**：读数与打分口径
        分离是更干净的解法，四个台账的手工项都能复用。
        """
        item = (defn.get("manual") or {}).get(spec["manual"]) or {}
        value = item.get(spec["value_field"])
        updated = item.get("updated_at")
        stale_days = int(item.get("stale_days", 120))
        stale = self._is_stale(updated, stale_days)
        label = spec.get("name") or item.get("label") or spec["key"]

        # 派生量：真正被打分的读数（缺关键字段时回退到原始 value）
        derived = None
        derive = spec.get("derive") or {}
        if derive:
            try:
                numerator = item.get(derive.get("of", "value"))
                denominator = item.get(derive.get("from", "peak"))
                if numerator is not None and denominator:
                    if derive.get("op") == "drop_pct":
                        derived = round((1 - numerator / denominator) * 100, 2)
            except Exception:  # 派生失败不能连累主流程，退回原始读数
                derived = None
        score_value = derived if derived is not None else value
        derive_metric = None
        for m in (spec.get("metrics") or []):
            if m and m != spec.get("metric"):
                derive_metric = m
                break

        if stale:
            warnings.append("%s 数据更新于 %s，已超 %d 天，请查阅最新资料后更新台账定义"
                            % (label, updated or "—", stale_days))
        updates = {spec["metric"]: value}
        if derive_metric:
            updates[derive_metric] = derived
        if spec.get("stale_metric"):
            updates[spec["stale_metric"]] = 1 if stale else 0

        if value is None:
            # 手工项缺失同样算「依据失效」，否则这个因子会悄悄从风险提示里消失
            if spec.get("stale_metric"):
                updates[spec["stale_metric"]] = 1
            warnings.append("%s 无可用数值（手工项未配置或已清空），相关判断失去依据" % label)
            return self._make_factor(
                key=spec["key"], name=label, value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失", source=spec.get("source", "手动配置"),
                asof=updated or "—", stale=True, note=item.get("source_note", ""),
                manual=True,
            ), updates

        template = spec.get("detail_template") or "{value}"
        detail = template.format_map(_SafeDict(
            {k: ("—" if v is None else v) for k, v in item.items()}))
        return self._make_factor(
            key=spec["key"], name=label, value=score_value, unit=spec.get("unit", ""),
            score=thesis_defs.score_by_table(score_value, thresholds[spec["thresholds_key"]]),
            detail=detail, source=spec.get("source", "手动配置"), asof=updated or "—",
            stale=stale, note=item.get("source_note", ""), manual=True,
        ), updates

    def _factor_style_relative(self, spec, ctx, defn, params, thresholds, warnings):
        """相对基准的超额收益（红利④）。spec: red / base、metric、thresholds_key。"""
        rel_long, rel_short, asof = self._relative_return(
            ctx["frames"].get(spec["red"]), ctx["frames"].get(spec["base"]), params)
        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=rel_long, unit=spec.get("unit", "pp"),
            score=thesis_defs.score_by_table(rel_long, thresholds[spec["thresholds_key"]]),
            detail=("近%d日：相对基准 %+.2fpp；近%d日 %+.2fpp"
                    % (int(params.get("style_lookback_days", 60)), rel_long,
                       int(params.get("style_lookback_short_days", 20)),
                       rel_short if rel_short is not None else 0.0))
            if rel_long is not None else "数据缺失",
            source=spec.get("source", "index_daily 自算"), asof=asof,
        )
        return factor, {spec["metric"]: rel_long}

    def _factor_crowding(self, spec, ctx, defn, params, thresholds, warnings):
        """拥挤度代理（红利⑤、恒生科技③）：点位对长期均线偏离度 + 股息率历史分位。

        `dy` 是可选的：红利低波走中证指数公司估值源，但港股指数（HSTECH/HSI）
        没有对应估值源，此时只用「点位偏离均线」这一维，分位跳过。
        原先写 `spec["dy"]` 会让不带 dy 的定义直接 KeyError → 因子被判「计算失败」。
        """
        ma_days = int(params.get("ma_days", 750))
        min_rank = int(params.get("min_samples_for_dy_rank", 120))
        red = ctx["frames"].get(spec["red"])
        dy_series = ctx["series"].get(spec.get("dy")) or []
        closes = self._closes(red)

        ext = None
        if closes and len(closes) >= _MIN_ROWS_FOR_CROWDING:
            window = closes[-ma_days:] if len(closes) > ma_days else closes
            ma = sum(window) / len(window)
            if ma > 0:
                ext = round((closes[-1] / ma - 1) * 100, 2)
        elif closes:
            warnings.append("历史不足 %d 个交易日，拥挤度无法计算" % _MIN_ROWS_FOR_CROWDING)

        dy_rank = None
        if len(dy_series) >= min_rank:
            dy_rank = _pct_rank([r["value"] for r in dy_series], dy_series[-1]["value"])

        if ext is None:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="数据缺失", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None, spec.get("rank_metric", "dy_rank"): dy_rank}

        detail = "点位高于近 %d 日均线 %.2f%%（延伸度越高越拥挤）" % (
            min(ma_days, len(closes)), ext)
        if not spec.get("dy"):
            # 该标的没有股息率数据源（如港股指数），只用偏离度一维，不要谎报「分位样本 0 期」
            detail += "；无股息率数据源，仅采用点位偏离口径"
        elif dy_rank is not None:
            detail += "；股息率历史分位 %.1f%%" % dy_rank
        else:
            detail += "；股息率分位样本 %d 期（需 ≥%d 期启用）" % (len(dy_series), min_rank)
        asof = str(red.iloc[-1]["trade_date"]) if red is not None and len(red) else "—"
        factor = self._make_factor(
            key=spec["key"], name=spec["name"], value=ext, unit=spec.get("unit", "%偏离"),
            score=thesis_defs.score_by_table(ext, thresholds[spec["thresholds_key"]]),
            detail=detail, source=spec.get("source", "—"), asof=asof,
            note=spec.get("note", ""),
        )
        return factor, {spec["metric"]: ext, spec.get("rank_metric", "dy_rank"): dy_rank}

    def _factor_index_shock(self, spec, ctx, defn, params, thresholds, warnings):
        """指数近 N 日涨跌幅 → 压力分档（债基⑤ 赎回压力的量化代理）。

        为什么用指数跌幅代理赎回压力：真实的基金净申赎没有公开日频数据
        （份额是季报/半年报口径，滞后且颗粒粗），但「净值下跌 → 赎回 →
        被迫抛售 → 再下跌」这条负反馈链的起点与表征都是价格。

        实测区分度（中证全债 H11001 近 5 日涨跌幅，2013-01~2026-09，n=3334）：
          常态 p50 = +0.11%；2013-06 钱荒 -0.90%；2016-12 债灾 -1.54%；
          2020-05 债灾 -0.93%；2022-11 理财赎回潮 -0.77%。
        即真正的负反馈都会跌破 -0.5%，而常态几乎不会 → 阈值可用固定档位。

        口径诚实性：detail 里显式标注「代理口径」，别让读者以为这是真实申赎数据。
        spec: frame（数据别名）、metric、unit、thresholds_key、shock_lookback_days
        """
        df = ctx["frames"].get(spec.get("frame"))
        closes = self._closes(df)
        n = int(spec.get("shock_lookback_days", params.get("shock_lookback_days", 5)))
        if len(closes) < n + 1:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="历史不足 %d 个交易日" % (n + 1),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        prev, latest = closes[-(n + 1)], closes[-1]
        if not prev:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="基期读数无效", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        chg = round((latest / prev - 1) * 100, 3)
        asof = max(self._date_close_map(df) or {"—": 0})
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=chg, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(chg, thresholds[spec["thresholds_key"]]),
            detail="近 %d 个交易日 %+.3f%%（代理口径：债市冲击，非真实申赎）" % (n, chg),
            source=spec.get("source", "index_daily 自算"), asof=asof,
            stale=self._index_is_stale(df, int(params.get("index_stale_days", 5))),
            note=spec.get("note", ""),
        ), {spec["metric"]: chg}

    # ── 数据装载辅助 ──────────────────────────────────────────────────

    def _factor_volume_percentile(self, spec, ctx, defn, params, thresholds, warnings):
        """指数成交额的近 N 日均值在自身历史中的分位（拥挤度的「资金维度」代理）。

        为什么用成交额：报告里拥挤度的资金维度是 ETF 规模与机构占比，但**源不可用** ——
        东财 `fund_etf_spot_em` 被本机代理拦截、交易所 `fund_etf_scale_*` 数据停在
        2025-01。指数成交额是能稳定拿到的、方向一致的替代：成交放量 ≈ 关注度上升 ≈ 拥挤。
        报告的对应结论是「A 股成交额与红利策略负相关——成交疲软时红利相对占优」。

        ⚠️ 口径限制：H30269 的 amount 自 2024-08 起才有（约 2 年），分位参考性有限，
        故台账里 confidence 标 mid；样本不足 `volume_min_samples` 时判数据缺失而不是硬算。

        spec: frame（数据别名）、metric、unit、thresholds_key、volume_ma_days
        """
        df = ctx["frames"].get(spec.get("frame"))
        if df is None or df.empty or "amount" not in getattr(df, "columns", []):
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "分位"),
                score=None, detail="该指数无成交额数据", source=spec.get("source", "—"),
                asof="—",
            ), {spec["metric"]: None}
        amts = [float(x) for x in df["amount"].tolist() if x is not None and float(x) > 0]
        n = int(spec.get("volume_ma_days", params.get("volume_ma_days", 20)))
        min_samples = int(params.get("volume_min_samples", 120))
        if len(amts) < n + min_samples:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "分位"),
                score=None, detail="成交额历史不足 %d 期（现 %d）" % (n + min_samples, len(amts)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        ma = [sum(amts[i - n + 1:i + 1]) / n for i in range(n - 1, len(amts))]
        cur = ma[-1]
        pct = round(100.0 * sum(1 for x in ma if x <= cur) / len(ma), 1)
        asof = max(self._date_close_map(df) or {"—": 0})
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=pct, unit=spec.get("unit", "分位"),
            score=thesis_defs.score_by_table(pct, thresholds[spec["thresholds_key"]]),
            detail="近 %d 日均成交额分位 %.1f%%（样本 %d 期，自 %s 起）"
                   % (n, pct, len(ma), str(min(self._date_close_map(df) or {"—": 0}))[:10]),
            source=spec.get("source", "index_daily 自算"), asof=asof,
            stale=self._index_is_stale(df, int(params.get("index_stale_days", 5))),
            note=spec.get("note", ""),
        ), {spec["metric"]: pct}

    # ── 黄金台账（gold_regime）专用：窗口变动 / 比值变动 / 跨频利差 / 位置分位 ──

    def _factor_pct_change_window(self, spec, ctx, defn, params, thresholds, warnings):
        """近 N 期**相对变动百分比**，直接作为读数（不放水平档）。

        与 `rate_level_trend` / `level_trend_pct` 的区别：那两个是「水平分 + 趋势修正」
        的双档结构；黄金的 ④ ETF 库存、⑥ 央行储备没有可用的绝对水平档
        （库存的「多少算多」随总盘子漂移，储备更是单向台阶），阈值只能作用在变动率上。

        spec: series、metric、unit、raw_unit、window_label（「个交易日」/「个月」）、
              lookback_periods、thresholds_key、label（展示用名词）
        """
        series = ctx["series"].get(spec["series"]) or []
        n = int(spec.get("lookback_periods", params.get("trend_lookback_days", 20)))
        label = spec.get("label") or spec.get("name", "")
        raw_unit = spec.get("raw_unit", "")
        win_label = spec.get("window_label", "期")
        if len(series) < n + 1:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="历史不足 %d %s" % (n + 1, win_label),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        cur, prev = series[-1]["value"], series[-(n + 1)]["value"]
        if not prev:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="基期读数无效", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        chg = round((cur / prev - 1) * 100, 2)
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=chg, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(chg, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g%s；近 %d %s %+.2f%%"
                   % (label, cur, (" " + raw_unit) if raw_unit else "", n, win_label, chg),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: chg, spec.get("raw_metric", spec["metric"] + "_raw"): cur}

    def _factor_ratio_pct_change(self, spec, ctx, defn, params, thresholds, warnings):
        """两条序列**按共同交易日**作比值，再取近 N 期的相对变动百分比（金银比）。

        为什么必须按共同交易日对齐：上海金与上海银是两个独立序列，各自最后一天
        未必相同（实测金 09-24、银 09-22）。若各取各自最新一行作比，等于把
        隔了两天的两个价格当成同一时点，比值会出现虚假跳变。

        spec: a / b（数据别名）、scale_a / scale_b（单位归一，如金 元/克 → 元/千克 要 ×1000）、
              ratio_unit、lookback_periods、window_label、thresholds_key、label_a / label_b
        """
        a_series = ctx["series"].get(spec["a"]) or []
        b_series = ctx["series"].get(spec["b"]) or []
        n = int(spec.get("lookback_periods", params.get("trend_lookback_days", 20)))
        sa = float(spec.get("scale_a", 1.0))
        sb = float(spec.get("scale_b", 1.0))
        win_label = spec.get("window_label", "期")

        a_map = {r["report_date"]: r["value"] for r in a_series if r["value"] is not None}
        b_map = {r["report_date"]: r["value"] for r in b_series if r["value"] is not None}
        common = sorted(set(a_map) & set(b_map))
        if len(common) < n + 1:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None,
                detail="共同交易日不足 %d 期（现 %d）" % (n + 1, len(common)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}

        def ratio(d):
            return a_map[d] * sa / (b_map[d] * sb)

        cur, prev = ratio(common[-1]), ratio(common[-(n + 1)])
        if not prev:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="基期比值无效", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        chg = round((cur / prev - 1) * 100, 2)
        ratio_unit = spec.get("ratio_unit", "")
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=chg, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(chg, thresholds[spec["thresholds_key"]]),
            # 用 label 而不是 name：name 带「⑤ 」这类序号前缀，拼进 detail 会变成
            # 「⑤ 金银比 57.44；近 20 …」（实测出现过），读起来像重复标号。
            detail="%s %.2f%s；近 %d %s %+.2f%%"
                   % (spec.get("label") or spec.get("name", "比值"),
                      cur, ratio_unit, n, win_label, chg),
            source=spec.get("source", "—"), asof=common[-1],
            stale=(self._is_stale(common[-1], ctx["stale_days"].get(spec["a"], 10))
                   or self._is_stale(common[-1], ctx["stale_days"].get(spec["b"], 10))),
            note=spec.get("note", ""),
        ), {spec["metric"]: chg, spec.get("raw_metric", spec["metric"] + "_raw"): round(cur, 2)}

    def _factor_spread_level_chg(self, spec, ctx, defn, params, thresholds, warnings):
        """两条**不同频率**序列作差后取最近两期变化（实际利率代理 = 美债10Y − CPI同比）。

        为什么要按月对齐：美债是日频、CPI 是月频，「取各自最新一期相减」会拿
        9 月的国债收益率减 8 月的 CPI（源最新一期 CPI 还没发布），口径错位一个月。
        这里按 YYYY-MM 归集——每月取该月**最后一个**国债读数，与该月 CPI 相减，
        再对月度序列取最近两期差（与原始报告的 ③ 完全同口径）。

        spec: a（日频序列别名）、b（月频序列别名）、metric、unit、thresholds_key、
              label_a / label_b、raw_metric
        """
        a_series = ctx["series"].get(spec["a"]) or []
        b_series = ctx["series"].get(spec["b"]) or []
        b_by_ym = {str(r["report_date"])[:7]: r["value"] for r in b_series
                   if r["value"] is not None}
        # 月份 → 该月的**完整日期**。asof 必须是 YYYY-MM-DD：`_is_stale` 用
        # strptime("%Y-%m-%d") 解析，给 "YYYY-MM" 会解析失败而**恒定判为陈旧**，
        # 页面上就会永远挂一个「⚠滞后」（实测量级：每次运行都误报）。
        b_date_by_ym = {str(r["report_date"])[:7]: str(r["report_date"])[:10]
                        for r in b_series if r["value"] is not None}
        monthly: dict = {}
        for r in a_series:
            if r["value"] is None:
                continue
            ym = str(r["report_date"])[:7]
            if ym in b_by_ym:  # 升序遍历 → 同月反复覆盖，留下该月最后一天
                monthly[ym] = round(r["value"] - b_by_ym[ym], 3)
        keys = sorted(monthly)
        if len(keys) < 2:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "pp"),
                score=None, detail="共同月份不足 2 期（现 %d）" % len(keys),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        last_ym, prev_ym = keys[-1], keys[-2]
        last, prev = monthly[last_ym], monthly[prev_ym]
        chg = round(last - prev, 3)
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=chg, unit=spec.get("unit", "pp"),
            score=thesis_defs.score_by_table(chg, thresholds[spec["thresholds_key"]]),
            detail="水平 %.3f%%（%s − %s）；环比 %+.3fpp（%s → %s）"
                   % (last, spec.get("label_a", "A"), spec.get("label_b", "B"),
                      chg, prev_ym, last_ym),
            source=spec.get("source", "—"),
            # 数据日期取**较粗那条腿**（月频）的日期，陈旧也按它的阈值判：
            # 这一对读数的口径上限就是「某月 CPI」，拿日频的 10 天窗口去卡它
            # 会让每个月的读数额外显示「⚠滞后」（月频发布本就滞后 1 个月）。
            asof=b_date_by_ym.get(last_ym) or last_ym,
            stale=self._is_stale(b_date_by_ym.get(last_ym) or last_ym,
                                 ctx["stale_days"].get(spec["b"], 45)),
            note=spec.get("note", ""),
        ), {spec["metric"]: chg, spec.get("raw_metric", spec["metric"] + "_raw"): last}

    def _factor_extension_rank(self, spec, ctx, defn, params, thresholds, warnings):
        """**位置维度**：点位相对长期均线的延伸度，在自身延伸度历史分布中的分位。

        为什么读数取「分位」而不是「延伸度 %」：原始报告的核心论点是「位置要放到
        自己的历史里看」——同样的 +27% 延伸度，在单边上涨期是常态、在震荡期是极值。
        用固定 % 档位会把这两者判成同一档（SOP 必踩坑 ①：阈值符号/量纲不可跨口径照搬）。

        与 `crowding` 的区别：那个输出**延伸度本身**并按固定档位给分（用于 A 股/港股
        指数）；这里输出**分位**，档位按 10/30/70/90 分位切。同源不同口径，不要互相照抄阈值。

        取数走 `ctx["series"]`（宏观表）而不是 `ctx["frames"]`（指数日线）：上海金不在
        `market_data_db.index_daily` 的标的清单里，硬走日线通道只会得到「日线不可用」。
        这带来一个刻意取舍——`macro_series` 的补数判定按日频工作日近似走，与指数日线
        的补数逻辑独立，不必为金价再造一套采集。

        spec: series（数据别名）、metric、raw_metric、ma_days、min_samples、thresholds_key、unit、label
        """
        series = ctx["series"].get(spec.get("series")) or []
        closes = [r["value"] for r in series if r["value"] is not None]
        label = spec.get("label") or "标的价格"
        ma_days = int(spec.get("ma_days", params.get("ma_days", 750)))
        min_n = int(spec.get("min_samples", params.get("min_samples_for_position_rank", 250)))
        need = ma_days + min_n
        if len(closes) < need:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None,
                detail="历史不足 %d 个交易日（现 %d），位置分位无法计算" % (need, len(closes)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}

        def ext_at(i):
            window = closes[i - ma_days + 1:i + 1]
            ma = sum(window) / len(window)
            return (closes[i] / ma - 1) * 100 if ma else None

        hist = [e for e in (ext_at(i) for i in range(ma_days - 1, len(closes)))
                if e is not None]
        ext_now = round(hist[-1], 2) if hist else None
        rank = _pct_rank(hist, ext_now) if ext_now is not None else None
        if rank is None:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None, detail="延伸度序列为空", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=rank, unit=spec.get("unit", "%分位"),
            score=thesis_defs.score_by_table(rank, thresholds[spec["thresholds_key"]]),
            detail="%s高于近 %d 日均线 %+.2f%%，处于历史 %.1f%% 分位（样本 %d 期）"
                   % (label, ma_days, ext_now, rank, len(hist)),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: rank, spec.get("raw_metric", spec["metric"] + "_ext"): ext_now}

    # ── 铜周期 / 猪周期台账新增计算器（2026-09-28） ────────────────────
    #
    # 共同特征：读数都是「比值」「分位」或「百分比偏离」，而不是某个绝对水平。
    # 这与 `extension_rank` 的取向一致——商品周期里「多少算高」随中枢漂移，
    # 绝对档位不可移植，故一律走相对口径。

    def _factor_ratio_level(self, spec, ctx, defn, params, thresholds, warnings):
        """两条序列**按共同日期**作比值，**比值本身**作为读数（猪粮比代理）。

        与 `ratio_pct_change` 的区别：那个输出「比值的变化率」（无量纲、只看方向），
        本计算器输出「比值本身」——猪粮比是一个**有绝对意义的水平量**（5:1 是官方
        一级预警线、6:1 约盈亏平衡），阈值必须直接作用在比值上。

        为什么要同源两条腿：猪粮比 = 生猪价 ÷ 玉米价，两条腿若取自不同源
        （如博亚和讯生猪 ÷ 搜猪网玉米），口径差会直接混进比值。故本台账的
        ① 刻意用「搜猪网生猪 ÷ 搜猪网玉米」。

        spec: a / b（数据别名）、metric、unit、label_a / label_b、thresholds_key、
              invert（可选：读数取负，用于「比值越低越有利」的语义翻转）
        """
        a_series = ctx["series"].get(spec["a"]) or []
        b_series = ctx["series"].get(spec["b"]) or []
        a_map = {r["report_date"]: r["value"] for r in a_series if r["value"] is not None}
        b_map = {r["report_date"]: r["value"] for r in b_series if r["value"] is not None}
        common = sorted(set(a_map) & set(b_map))
        if not common:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="两条序列无共同日期（现 A=%d / B=%d 期）"
                                   % (len(a_map), len(b_map)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        d = common[-1]
        if not b_map[d]:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", ""),
                score=None, detail="分母读数为 0，比值无效",
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        ratio = round(a_map[d] / b_map[d], 3)
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=ratio, unit=spec.get("unit", ""),
            score=thesis_defs.score_by_table(ratio, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g ÷ %s %.4g = %.3f（共同日期 %s）"
                   % (spec.get("label_a", "A"), a_map[d], spec.get("label_b", "B"),
                      b_map[d], ratio, d),
            source=spec.get("source", "—"), asof=d,
            # 两条腿各自的陈旧阈值都要满足——任一条腿过期，比值就不可信
            stale=(self._is_stale(d, ctx["stale_days"].get(spec["a"], 10))
                   or self._is_stale(d, ctx["stale_days"].get(spec["b"], 10))),
            note=spec.get("note", ""),
        ), {spec["metric"]: ratio, spec["a"] + "_level": a_map[d],
            spec["b"] + "_level": b_map[d]}

    def _factor_pct_rank_level(self, spec, ctx, defn, params, thresholds, warnings):
        """**位置维度**：序列最新读数在自身近 N 期历史中的分位（猪价历史分位）。

        与 `extension_rank` 的区别：那个先算「相对均线的延伸度」再取分位（衡量
        「贵不贵」，去趋势）；本计算器直接对**读数本身**取分位（衡量「高不高」，
        不去趋势）。猪价没有可靠的长期成本锚（玉米/豆粕成本随周期摆动），
        硬算「相对成本的延伸度」会引入第二个口径误差，故直接用价格分位。

        ⭐ **方向留意**：本因子输出的是「价格位置」这一客观读数，阈值表里
        「低分位 → 负分」是**刻意的**（价格低记负）。B 轴在汇总时再统一翻转成
        「B 正分 = 便宜 = 有利」——与 cu_regime 的 ⑤ 位置维度同构，
        不要以为阈值写反了。

        spec: series（数据别名）、metric、unit、lookback_periods、min_samples、
              invert、label、no_series_rank、thresholds_key
        """
        series = ctx["series"].get(spec.get("series")) or []
        vals = [r["value"] for r in series if r["value"] is not None]
        n = int(spec.get("lookback_periods", params.get("percentile_lookback", 580)))
        min_n = int(spec.get("min_samples", 60))
        label = spec.get("label") or spec.get("name", "读数")
        if len(vals) < min_n:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None, detail="样本不足 %d 期（现 %d），分位无法计算" % (min_n, len(vals)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        window = vals[-n:] if len(vals) > n else vals
        cur = vals[-1]
        rank = _pct_rank(window, cur)
        if rank is None:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None, detail="分位序列为空", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=rank, unit=spec.get("unit", "%分位"),
            score=thesis_defs.score_by_table(rank, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g，处于近 %d 期 %.1f%% 分位（样本 %d 期）"
                   % (label, cur, min(n, len(vals)), rank, len(window)),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: rank, spec.get("raw_metric", spec["metric"] + "_raw"): cur}

    def _factor_spread_pct(self, spec, ctx, defn, params, thresholds, warnings):
        """两条序列**按共同日期**作差后取相对百分比（期现价差 = 期货 ÷ 现货 − 1）。

        与 `spread_bp` / `spread_level_chg` 的区别：那两个是**同频**序列的绝对差
        或月度对齐差；本计算器处理**不同频**（期货日频 vs 现货周频）的比值型价差，
        且必须按共同日期对齐——各取最新一行会拿 09-28 的期货减 09-21 的现货。

        spec: a / b（数据别名）、metric、unit、label_a / label_b、thresholds_key
        """
        a_series = ctx["series"].get(spec["a"]) or []
        b_series = ctx["series"].get(spec["b"]) or []
        a_map = {r["report_date"]: r["value"] for r in a_series if r["value"] is not None}
        b_map = {r["report_date"]: r["value"] for r in b_series if r["value"] is not None}
        common = sorted(set(a_map) & set(b_map))
        if not common:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="两条序列无共同日期（现 A=%d / B=%d 期）"
                                   % (len(a_map), len(b_map)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        d = common[-1]
        if not b_map[d]:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="分母读数为 0，价差无效",
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        gap = round((a_map[d] / b_map[d] - 1) * 100, 2)
        state = "升水" if gap > 0 else ("贴水" if gap < 0 else "平水")
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=gap, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(gap, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g vs %s %.4g，%+.2f%%（%s，共同日期 %s）"
                   % (spec.get("label_a", "A"), a_map[d], spec.get("label_b", "B"),
                      b_map[d], gap, state, d),
            source=spec.get("source", "—"), asof=d,
            stale=(self._is_stale(d, ctx["stale_days"].get(spec["a"], 10))
                   or self._is_stale(d, ctx["stale_days"].get(spec["b"], 10))),
            note=spec.get("note", ""),
        ), {spec["metric"]: gap, spec["a"] + "_level": a_map[d],
            spec["b"] + "_level": b_map[d]}

    def _factor_vs_ma_gap(self, spec, ctx, defn, params, thresholds, warnings):
        """读数相对**序列自带的均线列**的百分比偏离（现货 vs 12 月均线）。

        为什么不复用 `level_trend_pct`：那是「水平分 + 趋势修正」的双档结构，
        而猪周期的位置判断只有「高于/低于长期均线」一维（原始脚本如此）。
        也不自己算均线：源直接提供 ma12 列，自算必然与源口径不一致。

        spec: series（数据别名，值=分子）、spec["b"] 可选（第二个数据别名为分母；
              缺省时用 `ma_field` 从同序列取——但长表只有单值，故**必须显式给 b**）、
              metric、raw_metric、unit、label、thresholds_key
        """
        a_series = ctx["series"].get(spec.get("a") or spec.get("series")) or []
        b_alias = spec.get("b")
        b_series = ctx["series"].get(b_alias) or [] if b_alias else []
        a_map = {r["report_date"]: r["value"] for r in a_series if r["value"] is not None}
        b_map = {r["report_date"]: r["value"] for r in b_series if r["value"] is not None}
        common = sorted(set(a_map) & set(b_map))
        if not common:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="读数与均线无共同日期（现 A=%d / B=%d 期）"
                                   % (len(a_map), len(b_map)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        d = common[-1]
        if not b_map[d]:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="均线读数为 0，偏离无效",
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        gap = round((a_map[d] / b_map[d] - 1) * 100, 2)
        label = spec.get("label") or spec.get("name", "读数")
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=gap, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(gap, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g vs 均线 %.4g = %+.2f%%（共同日期 %s）"
                   % (label, a_map[d], b_map[d], gap, d),
            source=spec.get("source", "—"), asof=d,
            stale=(self._is_stale(d, ctx["stale_days"].get(spec.get("a") or spec.get("series"), 10))
                   or (b_alias and self._is_stale(d, ctx["stale_days"].get(b_alias, 10)))),
            note=spec.get("note", ""),
        ), {spec["metric"]: gap, spec.get("raw_metric", spec["metric"] + "_raw"): b_map[d]}

    def _factor_vs_ma_rank(self, spec, ctx, defn, params, thresholds, warnings):
        """读数相对**自算长期均线**的百分比偏离（铜价 vs 年均线）。

        与 `vs_ma_gap` 的区别：猪价那条约 12 月均线由**源直接提供**，本计算器
        则由本地按 `ma_days` 自算（沪铜没有自带均线列）。两者不可混用：
        源口径自算必然有偏差。故铜台账 ⑥ 走本计算器、猪台账 ⑥ 走 vs_ma_gap。

        spec: series、metric、raw_metric、ma_days、unit、label、thresholds_key
        """
        series = ctx["series"].get(spec.get("series")) or []
        closes = [r["value"] for r in series if r["value"] is not None]
        ma_days = int(spec.get("ma_days", params.get("ma_days", 250)))
        label = spec.get("label") or "标的价格"
        if len(closes) < ma_days:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="历史不足 %d 期（现 %d）" % (ma_days, len(closes)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        ma = sum(closes[-ma_days:]) / ma_days
        if not ma:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%"),
                score=None, detail="均线读数为 0", source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        gap = round((closes[-1] / ma - 1) * 100, 2)
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=gap, unit=spec.get("unit", "%"),
            score=thesis_defs.score_by_table(gap, thresholds[spec["thresholds_key"]]),
            detail="%s %.4g 高于近 %d 日均线 %+.2f%%（均线 %.4g）"
                   % (label, closes[-1], ma_days, gap, ma),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: gap, spec.get("raw_metric", spec["metric"] + "_raw"): round(ma, 2)}

    def _factor_momentum_rank(self, spec, ctx, defn, params, thresholds, warnings):
        """近 N 期涨跌幅，在**自身历史同窗口涨跌幅分布**中的分位（20 日动量分位）。

        为什么要取分位而不是涨跌幅本身：商品/生猪的波动率随周期剧烈变化，
        固定的 ±5%/±10% 档位在低波动期几乎不命中、在高波动期频繁越界。
        取分位等于「用它的历史给自己定标尺」。

        ⭐ **方向由定义侧决定**：本计算器只输出分位；铜台账 ⑦ 的阈值表把
        「低分位 → 正分」（超卖有利，逆周期取向），这是定义的选择而非计算器的。

        spec: series、metric、raw_metric、lookback_periods、min_samples、
              unit、label、no_series_rank、thresholds_key
        """
        series = ctx["series"].get(spec.get("series")) or []
        vals = [r["value"] for r in series if r["value"] is not None]
        n = int(spec.get("lookback_periods", params.get("trend_lookback_days", 20)))
        min_n = int(spec.get("min_samples", params.get("min_samples_for_position_rank", 250)))
        if len(vals) < n + 1:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None, detail="历史不足 %d 期（现 %d）" % (n + 1, len(vals)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        hist = []
        for i in range(n, len(vals)):
            prev = vals[i - n]
            if prev:
                hist.append(round((vals[i] / prev - 1) * 100, 2))
        if len(hist) < min_n:
            return self._make_factor(
                key=spec["key"], name=spec["name"], value=None, unit=spec.get("unit", "%分位"),
                score=None,
                detail="动量历史样本不足 %d 期（现 %d），分位无法计算" % (min_n, len(hist)),
                source=spec.get("source", "—"), asof="—",
            ), {spec["metric"]: None}
        mom = hist[-1]
        rank = _pct_rank(hist, mom)
        asof = series[-1]["report_date"]
        return self._make_factor(
            key=spec["key"], name=spec["name"], value=rank, unit=spec.get("unit", "%分位"),
            score=thesis_defs.score_by_table(rank, thresholds[spec["thresholds_key"]]),
            detail="近 %d 期 %+.2f%%，处于历史 %.1f%% 分位（样本 %d 期）"
                   % (n, mom, rank, len(hist)),
            source=spec.get("source", "—"), asof=asof,
            stale=self._is_stale(asof, ctx["stale_days"].get(spec["series"], 10)),
            note=spec.get("note", ""),
        ), {spec["metric"]: rank, spec.get("raw_metric", spec["metric"] + "_raw"): mom}

    def _load_index(self, code: str, days: int, name: str, warnings: list,
                    label: str, params: dict):
        """读指数日线：**行数不足，或落后于最近应有交易日**，都触发一次采集，再读。

        只查行数是错的——库里可能有 500 行但停在两周前，那样相关因子
        会拿过期数据算出一个看起来很正常的分。

        这里刻意用 `_index_needs_refresh`（差 1 天就该补）而不是
        `index_stale_days`（默认 5 天）做**补数触发条件**：
        后者是「旧到该提示用户」的阈值，拿它当补数开关会让日线数据落后
        1~5 天都不刷新（2026-09-18 实测：库里停 9-17、今天 9-18，
        `_index_is_stale(df, 5)` 返回 False → 不采集）。陈旧提示仍按 `max_stale` 走。
        """
        import pandas as pd
        max_stale = int(params.get("index_stale_days", 5))

        df = None
        try:
            df = self.read_index_daily(code, days)
        except Exception as e:
            self.logger.warning("[Thesis] 读 %s 日线失败: %s", code, e)

        need_collect = (
            df is None
            or len(df) < _MIN_ROWS_FOR_CROWDING
            or self._index_needs_refresh(df)
        )
        if need_collect:
            try:
                self.collect_index_daily(code, days, name)
                refreshed = self.read_index_daily(code, days)
                if refreshed is not None and not refreshed.empty:
                    df = refreshed
            except Exception as e:
                self.logger.warning("[Thesis] 采集 %s 日线失败: %s", code, e)
                warnings.append("%s(%s) 日线采集失败：%s" % (label, code, str(e)[:80]))

        if df is None or df.empty:
            warnings.append("%s(%s) 日线不可用，相关因子无法计算" % (label, code))
            return pd.DataFrame()
        if self._index_is_stale(df, max_stale):
            latest = max(self._date_close_map(df) or {"—": 0})
            warnings.append("%s(%s) 日线最新只到 %s，已超 %d 天未更新，相关因子读数偏旧"
                            % (label, code, latest, max_stale))
        return df

    def _index_is_stale(self, df, max_days: int) -> bool:
        """陈旧判定：只用于**提示用户**读数偏旧，不用于决定要不要补数。"""
        dates = self._date_close_map(df)
        if not dates:
            return True
        return self._is_stale(max(dates), max_days)

    def _index_needs_refresh(self, df) -> bool:
        """补数判定：日线落后于最近应有交易日就该去抓。

        与 `_index_is_stale` 的分工见 `_load_index` docstring。
        """
        dates = self._date_close_map(df)
        if not dates:
            return True
        return self._needs_refresh(max(dates), "daily")

    @staticmethod
    def _closes(df) -> list:
        if df is None or df.empty or "close" not in df.columns:
            return []
        return [float(x) for x in df["close"].tolist() if x is not None]

    @staticmethod
    def _date_close_map(df) -> dict:
        if df is None or df.empty:
            return {}
        col = "trade_date" if "trade_date" in df.columns else (
            "report_date" if "report_date" in df.columns else None)
        if col is None:
            return {}
        out = {}
        for d, c in zip(df[col].tolist(), df["close"].tolist()):
            if c is None:
                continue
            out[str(d)[:10]] = float(c)
        return out

    def _relative_return(self, red, base, params):
        """相对基准的超额收益（长/短两个窗口）。返回 (rel_long, rel_short, asof)。"""
        long_n = int(params.get("style_lookback_days", 60))
        short_n = int(params.get("style_lookback_short_days", 20))
        m_red, m_base = self._date_close_map(red), self._date_close_map(base)
        common = sorted(set(m_red) & set(m_base))
        if len(common) <= long_n:
            asof = "—"
            if red is not None and len(red):
                asof = str(red.iloc[-1]["trade_date"])
            return None, None, asof

        def excess(n):
            d0, d1 = common[-(n + 1)], common[-1]
            return round((m_red[d1] / m_red[d0] - 1) * 100 - (m_base[d1] / m_base[d0] - 1) * 100, 2)

        rel_long = excess(long_n)
        rel_short = excess(short_n) if len(common) > short_n else None
        return rel_long, rel_short, common[-1]

    # ── 结论 / 信号 / 失效条件 ────────────────────────────────────────

    @staticmethod
    def _verdict(total: int, n_valid: int, verdicts: list) -> tuple[str, str]:
        if not n_valid:
            return "数据不足", "无法判断"
        # 分档按 min_total 降序，取第一个满足 total >= min_total 的档
        for v in sorted(verdicts, key=lambda x: x.get("min_total", 0), reverse=True):
            if total >= v.get("min_total", -99):
                return v.get("verdict", "—"), v.get("action", "—")
        return "数据不足", "无法判断"

    @staticmethod
    def _eval_signals(signals: dict, metrics: dict) -> dict:
        out = {}
        for group in ("buy", "warn"):
            rows = []
            for item in signals.get(group) or []:
                cur = metrics.get(item.get("metric"))
                # optional 指标（如股息率分位）样本不足时不展示，避免整条永远显示未命中
                if item.get("optional") and cur is None:
                    continue
                rows.append({
                    "label": item.get("label", ""),
                    "current": _fmt_metric(item.get("metric"), cur),
                    "hit": _compare(cur, item.get("op"), item.get("value")),
                })
            out[group] = rows
        return out

    @staticmethod
    def _eval_invalidation(invalidation: dict, metrics: dict) -> list[dict]:
        rows = []
        for rule in invalidation.get("rules") or []:
            cur = metrics.get(rule.get("metric"))
            rows.append({
                "text": rule.get("text", ""),
                "current": _fmt_metric(rule.get("metric"), cur),
                "hit": _compare(cur, rule.get("op"), rule.get("value")),
            })
        return rows

    # ── 小工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _eval_blocks(blocks: list, metrics: dict) -> list[dict]:
        """情景 / 传导路径：逐条评估 `when` 是否**全部**命中。

        与 signals 的区别：signals 是「命中即提示」，这里回答「当前落在哪一个情景」，
        所以前端会把命中的那条高亮、其余弱化——因此要逐条带出 `current`，
        让人能看见「差哪一条」，而不只是「没命中」。
        """
        out = []
        for b in blocks:
            conds = []
            for w in b.get("when") or []:
                m = w.get("metric")
                hit = _compare(metrics.get(m), w.get("op"), w.get("value"))
                conds.append({"metric": m, "op": w.get("op"), "value": w.get("value"),
                              "current": _fmt_metric(m, metrics.get(m)), "hit": bool(hit)})
            item = dict(b)
            item["conditions"] = conds
            item["hit"] = bool(conds) and all(c["hit"] for c in conds)
            out.append(item)
        return out

    @staticmethod
    def _eval_phases(phases: list, asof: str | None) -> list[dict]:
        """阶段定位：按数据口径日期落在哪个 from~to 区间，标注 `current`。

        阶段是月度粒度，比较 `YYYY-MM` 前缀即可；`to` 为空表示「至今」。
        """
        ym = str(asof or "")[:7]
        out = []
        for p in phases:
            frm, to = str(p.get("from") or ""), str(p.get("to") or "")
            item = dict(p)
            item["current"] = bool(ym) and ym >= frm and (not to or ym <= to)
            out.append(item)
        return out

    @staticmethod
    def _band_of(bands: list, value) -> str | None:
        """按 `[{name, min}]`（min 降序，取第一个 value >= min 的档）定位所属档。"""
        if value is None or not bands:
            return None
        for b in sorted(bands, key=lambda x: x.get("min", 0), reverse=True):
            if value >= b.get("min", 0):
                return b.get("name")
        return None

    @classmethod
    def _eval_stages(cls, cfg: dict, factors: list[dict], warnings: list) -> dict | None:
        """双维阶段矩阵：驱动力（若干因子之和）× 位置（单个因子的得分）→ 九宫格。

        为什么单开一块而不塞进 verdicts：verdicts 是**一维**的（总分 → 档位），
        而阶段判断本质是**二维**的——「驱动有利但位置已高」与「驱动有利且位置便宜」
        总分相近、含义完全相反（前者持有不追、后者是最好的买点）。
        线性总分给不出这个区分（原始报告的立论即在此）。

        与 `scenarios` / `transmission` 的关系：那两块也是「不参与打分」的研究表达，
        但它们的判据来自 `metrics` 字典；本块必须读**因子得分**（drive 是若干因子之和），
        所以单独实现。三块都不进 total。

        cfg: drive_keys（累加的因子 key）、pos_key（**单个因子**）或 pos_keys
             （**多个因子相加**，为猪周期的 B 轴而加：④+⑤+⑥ 三个因子共同构成
             「价格与预期」维度，只取 ④ 会把 B 轴缩水成 1/3，见下）、
             drive_bands / pos_bands（[{name,min}]）、cells（[{drive,pos,stage,desc,tone}]）

        **pos_key vs pos_keys**：黄金/铜的位置维度是**单个因子**（位置分位），故用
        `pos_key`；猪周期的 B 轴是**三个因子之和**（④猪价分位 + ⑤期现价差 + ⑥均线偏离），
        原始 hog_regime.py 的 `bb` 就是对这 3 个求和后分档（`if B <= -2: 价格低位`）。
        只取 ④ 会让 B 轴满分从 ±5 缩到 ±2、档位判定整体失真。两者互斥：
        声明 `pos_keys` 时按多项求和，否则回退 `pos_key` 单因子。

        为什么消费与驱动两轴都用「求和」而不引入加权：原始脚本就是等权求和
        （因子自身的 ±1/±2/±3 权重已经在阈值表里体现），引擎不需要再做一层加权。
        """
        if not cfg or not cfg.get("cells"):
            return None
        by_key = {f.get("key"): f for f in factors}
        drive_keys = list(cfg.get("drive_keys") or [])
        drive_valid, drive_missing = [], []
        for k in drive_keys:
            f = by_key.get(k)
            if f is not None and f.get("score") is not None:
                drive_valid.append(f["score"])
            else:
                drive_missing.append(k)
        drive_total = sum(drive_valid) if drive_valid else None

        # 位置维度：pos_keys（多项求和）优先，回退 pos_key（单因子）。
        # 两个键名都写进返回值，前端/测试不必关心用的是哪种。
        pos_keys = list(cfg.get("pos_keys") or [])
        if not pos_keys and cfg.get("pos_key"):
            pos_keys = [cfg.get("pos_key")]
        pos_valid, pos_missing = [], []
        for k in pos_keys:
            f = by_key.get(k)
            if f is not None and f.get("score") is not None:
                pos_valid.append(f["score"])
            else:
                pos_missing.append(k)
        pos_score = sum(pos_valid) if pos_valid else None
        pos_key = cfg.get("pos_key") or (pos_keys[0] if len(pos_keys) == 1 else None)

        drive_band = cls._band_of(cfg.get("drive_bands") or [], drive_total)
        pos_band = cls._band_of(cfg.get("pos_bands") or [], pos_score)

        cells = []
        stage = desc = tone = None
        for c in cfg["cells"]:
            is_cur = (drive_band is not None and pos_band is not None
                      and c.get("drive") == drive_band and c.get("pos") == pos_band)
            cells.append({"drive": c.get("drive"), "pos": c.get("pos"),
                          "stage": c.get("stage"), "desc": c.get("desc", ""),
                          "tone": c.get("tone", "neutral"), "current": bool(is_cur)})
            if is_cur:
                stage, desc, tone = c.get("stage"), c.get("desc"), c.get("tone")

        complete = not drive_missing and not pos_missing
        if drive_total is None or pos_score is None:
            warnings.append("阶段矩阵无法判定：驱动力或位置维度的读数缺失")
        elif not complete:
            # 部分因子缺失时照常给档（与 total 的口径一致），但必须说出来——
            # 缺一个 ±2 的因子足以跨档，闷着不说等于把「不完整」当「完整」。
            missing_all = drive_missing + pos_missing
            warnings.append("阶段矩阵只累加了 %d/%d 个因子（缺 %s），档位可能偏移"
                            % (len(drive_valid) + len(pos_valid),
                               len(drive_keys) + len(pos_keys),
                               "、".join(missing_all)))

        return {
            "drive_keys": drive_keys,
            "drive_total": drive_total,
            "drive_valid_count": len(drive_valid),
            "drive_factor_count": len(drive_keys),
            "drive_missing": drive_missing,
            "drive_band": drive_band,
            # 量纲由定义声明（默认「因子数 × 2」）。黄金②的 ⑥ 央行储备刻意是 ±1
            # （慢变量，报告明确「不用于择时」），真实满分是 11 而不是 12——
            # 页面上按 12 显示会让人以为还有 1 分没拿到。
            "drive_full": cfg.get("drive_full") or (len(drive_keys) * 2) or None,
            "pos_full": cfg.get("pos_full") or (len(pos_keys) * 2) or None,
            "pos_key": pos_key,
            "pos_keys": pos_keys,
            "pos_total": pos_score,
            "pos_valid_count": len(pos_valid),
            "pos_factor_count": len(pos_keys),
            "pos_missing": pos_missing,
            "pos_score": pos_score,
            "pos_band": pos_band,
            "complete": complete,
            "stage": stage,
            "desc": desc,
            "tone": tone,
            "bands_note": cfg.get("_说明", ""),
            "drive_bands": [{"name": b.get("name"), "min": b.get("min")}
                            for b in (cfg.get("drive_bands") or [])],
            "pos_bands": [{"name": b.get("name"), "min": b.get("min")}
                          for b in (cfg.get("pos_bands") or [])],
            "cells": cells,
        }

    @staticmethod
    def _max_asof(factors: list[dict]) -> str | None:
        """取所有因子数据日期里的最大值，作为快照的 `data_asof` 列。

        仅用于展示「这批读数取到哪一天的数据」；**不参与归档键**（归档按运行日，
        见 `thesis/storage.resolve_trade_date`）。各因子自己的数据日期见 `factors[*].asof`。
        """
        dates = []
        for f in factors:
            for token in str(f.get("asof") or "").split("/"):
                token = token.strip()[:10]
                try:
                    datetime.strptime(token, "%Y-%m-%d")
                    dates.append(token)
                except ValueError:
                    continue
        return max(dates) if dates else None

    def _is_stale(self, asof, threshold_days: int) -> bool:
        if not asof:
            return True
        try:
            d = datetime.strptime(str(asof)[:10], "%Y-%m-%d").date()
        except ValueError:
            return True
        return (self.today() - d).days > int(threshold_days)

    @staticmethod
    def _make_factor(key, name, value, unit, score, detail, source, asof,
                     stale=False, note="", manual=False) -> dict:
        """组装因子输出。

        `manual` 是给前端用的**来源标记**（不是质量标记）：手工维护的因子分数照常
        参与 total，页面只需据它把「人工填写」这件事显式标出来，避免读者以为
        所有读数都是当日自动取的。
        """
        status_map = {2: "很有利", 1: "有利", 0: "中性", -1: "偏不利", -2: "不利"}
        return {
            "key": key, "name": name, "value": value, "unit": unit,
            "score": score,
            "status": status_map.get(score, "数据缺失") if score is not None else "数据缺失",
            "detail": detail, "source": source, "asof": str(asof) if asof else "—",
            "stale": bool(stale), "note": note, "manual": bool(manual),
        }


# 注册表在类定义完成后回填（方法即类属性，直接引用最直观）
ThesisEngine.FACTOR_COMPUTERS = {
    "rate_premium": ThesisEngine._factor_rate_premium,
    "spread_bp": ThesisEngine._factor_spread_bp,
    "rate_level_trend": ThesisEngine._factor_rate_level_trend,
    "level_trend_pct": ThesisEngine._factor_level_trend_pct,
    "trend_bp": ThesisEngine._factor_trend_bp,
    "double_tightening": ThesisEngine._factor_double_tightening,
    "dual_axis": ThesisEngine._factor_dual_axis,
    "manual": ThesisEngine._factor_manual,
    "style_relative": ThesisEngine._factor_style_relative,
    "crowding": ThesisEngine._factor_crowding,
    "index_shock": ThesisEngine._factor_index_shock,
    "volume_percentile": ThesisEngine._factor_volume_percentile,
    # 黄金台账（gold_regime）
    "pct_change_window": ThesisEngine._factor_pct_change_window,
    "ratio_pct_change": ThesisEngine._factor_ratio_pct_change,
    "spread_level_chg": ThesisEngine._factor_spread_level_chg,
    "extension_rank": ThesisEngine._factor_extension_rank,
    # 铜周期台账（cu_regime）/ 猪周期台账（hog_regime）
    "ratio_level": ThesisEngine._factor_ratio_level,
    "pct_rank_level": ThesisEngine._factor_pct_rank_level,
    "spread_pct": ThesisEngine._factor_spread_pct,
    "vs_ma_gap": ThesisEngine._factor_vs_ma_gap,
    "vs_ma_rank": ThesisEngine._factor_vs_ma_rank,
    "momentum_rank": ThesisEngine._factor_momentum_rank,
}


def _pct_rank(series: list, value) -> float | None:
    """value 在 series 中的分位（0-100）。"""
    xs = [x for x in series if x is not None]
    if not xs or value is None:
        return None
    k = sum(1 for x in xs if x <= value)
    return round(100.0 * k / len(xs), 1)


def _compare(current, op: str, threshold) -> bool:
    if current is None:
        return False
    try:
        if op == "is_true":
            return bool(current)
        if op == ">":
            return current > threshold
        if op == "<":
            return current < threshold
        if op == ">=":
            return current >= threshold
        if op == "<=":
            return current <= threshold
    except TypeError:
        return False
    return False


_METRIC_FMT = {
    "premium": ("pp", 2), "dy": ("%", 2), "cn10_y": ("%", 4),
    "crowding": ("%", 2), "style_rel": ("pp", 2),
    "insurance_gap": (" 亿元", 0), "dy_rank": ("%", 1),
    "us10_y": ("%", 4), "shibor_1w": ("%", 4),
    "cn10_chg_bp": ("bp", 1), "us10_chg_bp": ("bp", 1),
    "usdcny": ("", 2), "gov_supply_yi": (" 亿元", 0),
    "double_tighten": ("", 0),
    # v1.1：redemption_stress（手工 0/1/2 级）已废弃 → bond_shock_5d（近5日涨跌幅%）；
    # dollar_liq（手工档位）已废弃 → hk_hibor_1w（HIBOR 1W %）
    "bond_shock_5d": ("%", 2), "hk_hibor_1w": ("%", 4),
}


def _fmt_metric(metric: str, value) -> str:
    if value is None:
        return "—"
    if metric.endswith("_stale"):
        return "已陈旧" if value else "正常"
    unit, nd = _METRIC_FMT.get(metric, ("", 2))
    try:
        s = ("%." + str(nd) + "f") % float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit in ("pp", "%偏离", "bp") and float(value) > 0:
        s = "+" + s
    return s + unit
