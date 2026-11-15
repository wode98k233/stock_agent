"""定价目录配置服务（设计文档 docs/superpowers/specs/2026-10-13-report-cost-estimation-design.md §9.3/§9.4/§9.6）。

职责：读 / 校验 / 备份 / 原子写 / 升版本 / 热失效。**不含任何计价逻辑** —— 计价只在
utils/pricing.py（单一事实源），本模块只搬运与校验。

为什么价目是文件而不是 .env：供应商 → 模型 → 峰谷价是**嵌套表**，塞进 .env 既没法维护
也没法版本化/备份。范式与 report_templates / route_rules.json 一致（文件 + 专属 API + 专属面板）。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from utils import pricing as pricing

_BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "backups"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TIERS = ("off_peak", "peak")
_PRICE_KEYS = ("in_miss", "in_hit", "out")
_BILLINGS = ("paid", "free", "unknown")
# 按次计费服务目前只支持「每次请求」一个计价单位（Search/Fetch 都是按次一口价，不分峰谷）
_SERVICE_UNIT = "per_request"


# ============================================================
# 读
# ============================================================

def runtime_path() -> Path:
    """运行时价目路径（页面编辑的目标文件）。"""
    raw = str(getattr(pricing, "_cfg", lambda *a: "")("COST_PRICING_CATALOG_PATH", "") or "").strip()
    p = Path(raw) if raw else Path(pricing.RUNTIME_CATALOG_REL)
    return p if p.is_absolute() else pricing.project_root() / p


def read_catalog() -> Dict[str, Any]:
    """读盘上的价目（走 pricing 的加载器：运行时优先、缺失回落默认）。"""
    catalog = dict(pricing.load_catalog(force=True))
    runtime = runtime_path()
    return {
        "catalog": {k: v for k, v in catalog.items() if not k.startswith("_")},
        "path": str(runtime),
        "default_path": str(pricing.default_catalog_path()),
        "is_default": Path(str(catalog.get("_path") or "")) == pricing.default_catalog_path(),
        "overridden": runtime.exists(),
        "mtime": runtime.stat().st_mtime if runtime.exists() else 0.0,
    }


# ============================================================
# 校验（服务端为准，前端同步提示）
# ============================================================

def validate_catalog(catalog: Any) -> Dict[str, List[str]]:
    """返回 {errors, warnings}：errors 非空即拒绝保存（§9.4）。"""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(catalog, dict):
        return {"errors": ["价目顶层必须是对象"], "warnings": []}

    for key in ("version", "currency", "unit"):
        if not str(catalog.get(key) or "").strip():
            errors.append("缺少 " + key + " 字段")

    schedule = catalog.get("peak_schedule")
    if not isinstance(schedule, dict):
        errors.append("peak_schedule 必须是对象（时区 / 高峰星期 / 高峰时段）")
        schedule = {}
    if not str(schedule.get("tz") or "").strip():
        errors.append("peak_schedule.tz 不能为空（DeepSeek 以 Asia/Shanghai 为准）")
    weekdays = schedule.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays:
        errors.append("peak_schedule.weekdays 必须是非空数组（1=周一 … 7=周日）")
    else:
        for d in weekdays:
            if not isinstance(d, int) or not 1 <= d <= 7:
                errors.append("peak_schedule.weekdays 只接受 1~7 的整数（收到 %r）" % (d,))
                break
    windows = schedule.get("windows")
    spans: List[Tuple[int, int]] = []
    if not isinstance(windows, list):
        errors.append("peak_schedule.windows 必须是数组（形如 [[\"09:00\",\"12:00\"]]）")
    else:
        for win in windows:
            if not isinstance(win, (list, tuple)) or len(win) != 2:
                errors.append("高峰时段必须是 [开始, 结束] 两元素（收到 %r）" % (win,))
                continue
            m1, m2 = _HHMM.match(str(win[0] or "")), _HHMM.match(str(win[1] or ""))
            if not m1 or not m2:
                errors.append("高峰时段必须是 HH:MM 24 小时制（收到 %r-%r）" % (win[0], win[1]))
                continue
            a = int(m1.group(1)) * 60 + int(m1.group(2))
            b = int(m2.group(1)) * 60 + int(m2.group(2))
            if a >= b:
                errors.append("高峰时段必须开始早于结束（收到 %s-%s）" % (win[0], win[1]))
                continue
            spans.append((a, b))
    spans.sort()
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            warnings.append("高峰时段存在重叠，重叠部分仍只算一次高峰价")

    mult = schedule.get("peak_multiplier")
    if mult is not None:
        try:
            if float(mult) <= 0:
                errors.append("peak_multiplier 必须大于 0")
        except (TypeError, ValueError):
            errors.append("peak_multiplier 必须是数字")

    providers = catalog.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("providers 必须是非空对象（至少一个供应商）")
        providers = {}

    for name, prov in providers.items():
        # ⚠️ 别名唯一性只在**同一供应商内**成立：同一个模型名可以由官方与中转同时提供，
        # 到底用哪套价由 base_url 的 host 决定（utils/pricing.resolve_provider）。
        # 早期版本做成全局唯一，结果「官方 + 中转」这种最常见的配置直接存不进去。
        alias_owner: Dict[str, str] = {}
        tag = "供应商 " + str(name)
        if not str(name or "").strip():
            errors.append("供应商名不能为空")
        if not isinstance(prov, dict):
            errors.append(tag + " 必须是对象")
            continue
        billing = str(prov.get("billing") or "")
        if billing not in _BILLINGS:
            errors.append(tag + " 的 billing 只能是 paid / free / unknown（收到 %r）" % (billing,))
        hosts = prov.get("hosts")
        if hosts is not None:
            if not isinstance(hosts, list) or any(not str(h or "").strip() for h in hosts):
                errors.append(tag + " 的 hosts 必须是字符串数组（如 [\"api.deepseek.com\"]）")
        until = prov.get("valid_until")
        if until:
            try:
                date.fromisoformat(str(until))
            except ValueError:
                errors.append(tag + " 的 valid_until 必须是 YYYY-MM-DD（收到 %r）" % (until,))
        elif billing == "free":
            warnings.append(tag + " 是免费源但没有填有效期：额度变化时不会自动提醒")

        models = prov.get("models")
        if models is None:
            models = {}
        if not isinstance(models, dict):
            errors.append(tag + " 的 models 必须是对象（模型 key → 价目）")
            continue
        if billing == "paid" and not models:
            warnings.append(tag + " 是付费源但没有任何模型价目：它的调用会被记成「未计价」")
        for mkey, entry in models.items():
            mtag = tag + " / 模型 " + str(mkey)
            if not str(mkey or "").strip():
                errors.append(tag + " 存在空模型名")
            if not isinstance(entry, dict):
                errors.append(mtag + " 必须是对象")
                continue
            if entry.get("free") is True:
                continue
            names = [str(mkey)] + [str(a) for a in (entry.get("aliases") or [])]
            for nm in names:
                low = pricing.normalize_model(nm)
                if not low:
                    errors.append(mtag + " 存在空别名")
                    continue
                owner = alias_owner.get(low)
                if owner and owner != mtag:
                    errors.append("别名 %s 同时属于 %s 与 %s（同一写法只能有一个价）" % (nm, owner, mtag))
                else:
                    alias_owner[low] = mtag
            for tier in _TIERS:
                raw = entry.get(tier)
                if raw is None:
                    continue
                if not isinstance(raw, dict):
                    errors.append(mtag + " 的 " + tier + " 必须是对象（in_miss/in_hit/out）")
                    continue
                for pk in _PRICE_KEYS:
                    if pk not in raw:
                        continue
                    try:
                        val = float(raw.get(pk))
                    except (TypeError, ValueError):
                        errors.append(mtag + " " + tier + "." + pk + " 必须是数字")
                        continue
                    if val < 0:
                        errors.append(mtag + " " + tier + "." + pk + " 不能为负")
            off, peak = entry.get("off_peak"), entry.get("peak")
            if isinstance(off, dict) and isinstance(peak, dict):
                try:
                    ratio = float(schedule.get("peak_multiplier") or 2.0)
                    for pk in _PRICE_KEYS:
                        if pk in off and pk in peak and float(off[pk]) > 0:
                            expect = float(off[pk]) * ratio
                            if abs(float(peak[pk]) - expect) > 1e-9:
                                warnings.append("%s 的 %s 峰价 %.4g 不等于谷价 × %.2g（%.4g）"
                                                % (mtag, pk, float(peak[pk]), ratio, expect))
                                break
                except (TypeError, ValueError):
                    pass

    # 按次计费的服务（Search / Fetch 这类不按 token 计价的外部调用，2026-10-18 加）。
    # ⚠️ 它**不属于** providers/models 那套 token 价目：单位是「元/次」，没有峰谷，
    # 计价走 utils/pricing.calc_service_cost，落库在 tool step 的 extra.cost(kind='service')。
    services = catalog.get("services")
    if services is not None:
        if not isinstance(services, dict):
            errors.append("services 必须是对象（服务名 → 按次价目，如 deepseek-search）")
        else:
            for sname, entry in services.items():
                stag = "按次服务 " + str(sname)
                if not str(sname or "").strip():
                    errors.append("services 存在空服务名")
                if not isinstance(entry, dict):
                    errors.append(stag + " 必须是对象")
                    continue
                billing = str(entry.get("billing") or "")
                if billing not in _BILLINGS:
                    errors.append(stag + " 的 billing 只能是 paid / free / unknown（收到 %r）" % (billing,))
                unit = str(entry.get("unit") or "")
                if unit != _SERVICE_UNIT:
                    errors.append(stag + " 的 unit 目前只支持 " + _SERVICE_UNIT + "（收到 %r）" % (unit,))
                try:
                    price = float(entry.get("price"))
                except (TypeError, ValueError):
                    errors.append(stag + " 的 price 必须是数字（元 / 次）")
                else:
                    if price < 0:
                        errors.append(stag + " 的 price 不能为负")
                    elif price == 0 and billing == "paid":
                        warnings.append(stag + " 是付费服务但单价为 0：调用会按 ¥0 计账，确认是否漏填")

    fallback = catalog.get("fallback")
    if fallback is not None and not isinstance(fallback, dict):
        errors.append("fallback 必须是对象（如 {\"billing\":\"unknown\",\"policy\":\"warn\"}）")

    return {"errors": _dedup(errors), "warnings": _dedup(warnings)}


def _dedup(items: List[str]) -> List[str]:
    out: List[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


# ============================================================
# 写
# ============================================================

def _next_version(old: str, now: datetime) -> str:
    """版本号 = 日期；同一天再保存则追加 -2 / -3 …（快照可追溯）。"""
    today = now.strftime("%Y-%m-%d")
    old = str(old or "").strip()
    if not old.startswith(today):
        return today
    suffix = old[len(today):].lstrip("-")
    try:
        n = int(suffix) + 1
    except ValueError:
        n = 2
    return today + "-" + str(n)


def save_catalog(catalog: Any) -> Dict[str, Any]:
    """校验 → 备份 → 原子写 → 升版本 → 热失效。非法结构抛 ValueError（路由层转 400）。"""
    result = validate_catalog(catalog)
    if result["errors"]:
        raise ValueError("；".join(result["errors"]))

    target = runtime_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = _BACKUP_DIR / ("pricing_catalog." + stamp + ".json")
        shutil.copy2(target, backup)

    payload = dict(catalog)
    payload["version"] = _next_version(str(catalog.get("version") or ""), datetime.now())
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:   # 固定 LF，避免行尾翻转
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(target)                 # 原子替换：避免半截文件被计价读到
    pricing.invalidate()                # 热失效：下次 LLM 调用即用新价，无需重启
    return {"success": True, "version": payload["version"], "path": str(target),
            "backup": str(backup) if backup else "",
            "warnings": result["warnings"]}


def reset_catalog() -> Dict[str, Any]:
    """从仓库内置默认价目恢复（同样先备份当前运行时文件）。"""
    default_path = pricing.default_catalog_path()
    if not default_path.exists():
        raise ValueError("默认价目文件不存在：" + str(default_path))
    with open(default_path, encoding="utf-8") as fh:
        catalog = json.load(fh)
    return save_catalog(catalog)


# ============================================================
# 试算
# ============================================================

def trial(payload: Dict[str, Any]) -> Dict[str, Any]:
    """试算：给「模型 + 供应商/host + token 数 + 时刻」，同时返回闲时/峰时两档成本。

    为什么要两档：峰谷配错（比如时区写成 UTC）时，两档数字会一起错得离谱，
    并排展示才看得出来；只给一档的话「刚配的价目是否生效」根本无从验证。
    """
    payload = payload or {}
    model = str(payload.get("model") or "")
    provider = str(payload.get("provider") or "")
    base_url = str(payload.get("base_url") or "")
    ts = payload.get("ts")
    usage = {
        "input_tokens": _int(payload.get("input_tokens")),
        "cached_tokens": _int(payload.get("cached_tokens")),
        "output_tokens": _int(payload.get("output_tokens")),
    }
    if ts in (None, ""):
        ts = datetime.now().timestamp()
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = datetime.now().timestamp()

    quote = pricing.resolve_quote(model=model, provider=provider, base_url=base_url)
    peak_ts = _ts_at_peak(quote.schedule)
    off = pricing.calc_cost(usage, quote, peak_ts[0])
    peak = pricing.calc_cost(usage, quote, peak_ts[1])
    now_cost = pricing.calc_cost(usage, quote, ts)
    return {
        "provider": quote.provider,
        "model": quote.model,
        "billing": quote.billing,
        "currency": quote.currency,
        "catalog_version": quote.catalog_version,
        "now": now_cost.as_dict(),
        "off_peak": off.as_dict(),
        "peak": peak.as_dict(),
        "peak_windows": quote.schedule.get("windows") or [],
        "peak_tz": quote.schedule.get("tz") or "",
        "note": "免费源按 0 计；未计价（billing=unknown）表示价目里没有它，请补价目",
    }


def _int(val: Any) -> int:
    try:
        return max(0, int(float(val or 0)))
    except (TypeError, ValueError):
        return 0


def _ts_at_peak(schedule: Dict[str, Any]) -> Tuple[float, float]:
    """造两个时间戳：一个落在闲时、一个落在高峰（供两档试算）。

    直接扫未来 8 天，取第一个高峰窗口内的时刻；扫不到（没配窗口）就都给同一时刻。
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    tz_name = str((schedule or {}).get("tz") or "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    base = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    windows = (schedule or {}).get("windows") or []
    weekdays = set(int(d) for d in ((schedule or {}).get("weekdays") or [1, 2, 3, 4, 5]))
    off_ts = base.timestamp()
    peak_ts = off_ts
    for day in range(0, 8):
        cur = base + timedelta(days=day)
        if cur.isoweekday() not in weekdays:
            continue
        for win in windows:
            try:
                hh, mm = str(win[0]).split(":")
                cand = cur.replace(hour=int(hh), minute=int(mm))
            except Exception:
                continue
            peak_ts = cand.timestamp()
            break
        else:
            continue
        break
    # 闲时基准：找一个非高峰时刻（周末中午最稳）
    for day in range(0, 8):
        cur = base + timedelta(days=day)
        if cur.isoweekday() in weekdays:
            continue
        off_ts = cur.replace(hour=12, minute=0).timestamp()
        break
    return off_ts, peak_ts
