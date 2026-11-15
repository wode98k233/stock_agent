"""报告成本计价内核（纯函数 + 版本化价目）。

设计文档：docs/superpowers/specs/2026-10-13-report-cost-estimation-design.md（§5 / §9.5 / §10）

## 为什么单独一个模块

成本只有一个事实源：**记录时按「(model, tokens, 调用时刻, 供应商) → 元」算一次，把结果连同
当时用的单价快照一起落库**（§5.1）。这样：

- 峰谷按**调用时刻**判定，准；
- 价目日后调价**不会改写历史账单**（快照可追溯）；
- 聚合退化成 SUM，快、稳。

所以本模块只做四件纯事，不做 IO 之外的花活：读价目（带缓存）、判峰谷、归一名、算钱。

## 价目为什么是数据文件

DeepSeek 2026 年已至少两次调价（5/31 降、8/17 涨、9/10 峰谷化）。价格是**会变的外部事实**，
不能写死在代码里：运行时价目 `data/pricing/pricing_catalog.json`（页面可编辑、不进 git），
仓库内置默认 `data/pricing/pricing_catalog.default.json`（随代码走、reset 的源）。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_UNIT = 1_000_000.0
_FALLBACK_TZ = "Asia/Shanghai"
_DEFAULT_WEEKDAYS = [1, 2, 3, 4, 5]

# 运行时价目（页面可编辑）优先；缺失回落仓库内置默认（保证「删了也能跑」）
RUNTIME_CATALOG_REL = "data/pricing/pricing_catalog.json"
DEFAULT_CATALOG_REL = "data/pricing/pricing_catalog.default.json"

# 价目缓存：(path, mtime, version) → catalog。PUT 之后调 invalidate() 即热生效。
_CACHE: Dict[str, Any] = {"key": None, "catalog": None}
_warned: set = set()          # 未知模型/过期免费源：同一 key 只告警一次（§10）
_DOWNGRADED: set = set()      # 已降级为 unknown 的免费源


def project_root() -> Path:
    """仓库/安装根目录（utils/pricing.py → parents[1]）。"""
    return Path(__file__).resolve().parents[1]


def _cfg(name: str, default: Any) -> Any:
    """读配置（延迟 import，避免 config ↔ pricing 循环依赖）。"""
    try:
        from config import Config
        val = getattr(Config, name, None)
        return default if val in (None, "") else val
    except Exception:
        return default


def cost_enabled() -> bool:
    """成本计算总开关（COST_TRACKING_ENABLED，默认开）。"""
    val = _cfg("COST_TRACKING_ENABLED", True)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def default_base_url() -> str:
    """当前 chat 端点的 base_url（供应商识别兜底用）。

    ⚠️ 只用于「这是哪个供应商」，不参与计价。网关若切到别的 profile（不同 host），
    成本会落到 unknown —— 宁可显示「未计价」，也不猜一个价（设计文档 §5.2）。
    """
    return str(_cfg("OPENAI_API_BASE", "") or "")


def catalog_path() -> Path:
    """运行时价目路径（缺失则回落默认价目）。"""
    raw = str(_cfg("COST_PRICING_CATALOG_PATH", "") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else project_root() / p
    runtime = project_root() / RUNTIME_CATALOG_REL
    if runtime.exists():
        return runtime
    return project_root() / DEFAULT_CATALOG_REL


def default_catalog_path() -> Path:
    return project_root() / DEFAULT_CATALOG_REL


def load_catalog(force: bool = False) -> dict:
    """读价目（缓存键 = 路径 + mtime + version）。

    运行时文件缺失/损坏 → 回落默认价目（并告警一次），绝不让成本计算把主流程拖垮。
    """
    path = catalog_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(path), mtime)
    if not force and _CACHE["key"] == key and _CACHE["catalog"] is not None:
        return _CACHE["catalog"]

    catalog = _read_catalog(path)
    if catalog is None and path != default_catalog_path():
        logger.warning("[计价] 运行时价目不可用（%s），回落默认价目", path)
        path = default_catalog_path()
        catalog = _read_catalog(path) or {}
    catalog = dict(catalog or {})
    catalog["_path"] = str(path)
    _CACHE["key"] = key
    _CACHE["catalog"] = catalog
    return catalog


def _read_catalog(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("[计价] 价目解析失败（%s）：%s", path, exc)
        return None


def invalidate() -> None:
    """保存价目后调用：下次取价目即读新文件（无需重启）。"""
    _CACHE["key"] = None
    _CACHE["catalog"] = None


# ============================================================
# 峰谷
# ============================================================

def _schedule_of(schedule: Optional[dict] = None) -> dict:
    if schedule:
        return schedule
    cat = load_catalog()
    sch = cat.get("peak_schedule")
    return sch if isinstance(sch, dict) else {}


def _tz_of(schedule: dict) -> ZoneInfo:
    name = str((schedule or {}).get("tz") or _cfg("COST_PEAK_TZ", _FALLBACK_TZ) or _FALLBACK_TZ)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(_FALLBACK_TZ)


def _parse_hhmm(text: Any):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


def is_peak(ts: Optional[float] = None, schedule: Optional[dict] = None) -> bool:
    """调用时刻是否落在高峰时段。

    ⚠️ 必须用**北京时间**判定（DeepSeek 以北京时间为准）：UTC 窗口会变成
    01:00–04:00 / 06:00–10:00，极易配错。
    """
    sch = _schedule_of(schedule)
    windows = sch.get("windows") or []
    if not windows:
        return False
    tz = _tz_of(sch)
    try:
        dt = datetime.fromtimestamp(float(ts if ts is not None else time.time()), tz)
    except Exception:
        dt = datetime.now(tz)
    weekdays = sch.get("weekdays") or _DEFAULT_WEEKDAYS
    try:
        if dt.isoweekday() not in {int(d) for d in weekdays}:
            return False
    except Exception:
        return False
    cur = dt.hour * 60 + dt.minute
    for win in windows:
        if not isinstance(win, (list, tuple)) or len(win) < 2:
            continue
        start, end = _parse_hhmm(win[0]), _parse_hhmm(win[1])
        if start is None or end is None or start >= end:
            continue
        if start <= cur < end:      # 半开区间 [start, end)
            return True
    return False


# ============================================================
# 模型归一 / 供应商解析
# ============================================================

def normalize_model(name: str) -> str:
    """模型名归一：小写、去空格、去供应商前缀、去日期/版本后缀。

    「deepseek-flash / deepseek-v4-flash / DeepSeek v4.1 Flash」这类写法必须收敛到同一价目，
    否则每次改名都要改代码（§3 别名归一）。
    """
    s = str(name or "").strip().lower()
    if not s:
        return ""
    s = s.split("/")[-1]                  # 去掉 vendor/ 前缀
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", s)   # 去掉日期后缀
    return s


def _host_of(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^[a-zA-Z]+://", "", raw)
    host = raw.split("/")[0].split("@")[-1]
    if ":" in host:
        host = host.split(":")[0]
    return host.lower()


def _host_matches(host: str, patterns: List[str]) -> bool:
    if not host:
        return False
    for pat in patterns or []:
        p = str(pat or "").strip().lower()
        if not p:
            continue
        if p == host:
            return True
        if p.startswith("*.") and host.endswith(p[1:]):
            return True
        if "/" in p:                       # CIDR（内网中转）
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(p, strict=False):
                    return True
            except Exception:
                continue
    return False


def _providers(catalog: Optional[dict] = None) -> Dict[str, dict]:
    cat = catalog if catalog is not None else load_catalog()
    provs = cat.get("providers")
    return provs if isinstance(provs, dict) else {}


def _free_expired(provider: dict) -> bool:
    """免费源是否已过有效期（免费是**带有效期的状态**，不是常量）。"""
    if str(provider.get("billing") or "") != "free":
        return False
    if not _cfg("COST_FREE_VALIDITY_CHECK", True):
        return False
    until = str(provider.get("valid_until") or "").strip()
    if not until:
        return False
    try:
        return datetime.now(ZoneInfo(_FALLBACK_TZ)).date().isoformat() > until
    except Exception:
        return False


def billing_of(provider_name: str, provider: Optional[dict] = None,
               catalog: Optional[dict] = None) -> str:
    prov = provider if provider is not None else _providers(catalog).get(provider_name) or {}
    billing = str(prov.get("billing") or "unknown")
    if _free_expired(prov):
        if provider_name not in _DOWNGRADED:
            _DOWNGRADED.add(provider_name)
            logger.warning("[计价] 免费源 %s 已过有效期（valid_until=%s），按未计价处理",
                           provider_name, prov.get("valid_until"))
        return "unknown"
    return billing


def check_free_validity(force: bool = False) -> dict:
    """免费源有效期巡检（启动时 + 每日）：过期的自动降级为「未计价」并告警。

    免费额度会变，但**不能靠代码常量判断**——这里只读价目文件里的 valid_until，
    过期即降级 + 一次 WARNING（去重靠 _DOWNGRADED，同一进程同一源只报一次）。

    返回 {checked, expired: [names], providers: [...]}，供巡检作业写 summary。
    """
    invalidate()
    cat = load_catalog(force=True)
    out = {"checked": 0, "expired": [], "providers": [], "version": str(cat.get("version") or "")}
    for name, prov in _providers(cat).items():
        if not isinstance(prov, dict):
            continue
        billing = str(prov.get("billing") or "unknown")
        until = str(prov.get("valid_until") or "")
        out["checked"] += 1
        out["providers"].append({"name": str(name), "billing": billing, "valid_until": until})
        if billing != "free":
            continue
        if _free_expired(prov):
            if force:
                _DOWNGRADED.discard(str(name))
            billing_of(str(name), prov, cat)     # 触发一次降级告警
            out["expired"].append(str(name))
    return out


def resolve_provider(model: str = "", base_url: str = "",
                     catalog: Optional[dict] = None) -> str:
    """供应商解析：显式 provider 优先，其次由 base_url 的 host 反查价目 hosts[]。"""
    provs = _providers(catalog)
    host = _host_of(base_url)
    if host:
        for name, prov in provs.items():
            if _host_matches(host, list((prov or {}).get("hosts") or [])):
                return str(name)
    return ""


@dataclass
class PriceQuote:
    provider: str = ""
    model: str = ""
    billing: str = "unknown"
    prices: Dict[str, Dict[str, float]] = field(default_factory=dict)
    schedule: Dict[str, Any] = field(default_factory=dict)
    catalog_version: str = ""
    currency: str = "CNY"

    def price_of(self, tier: str) -> Dict[str, float]:
        """取某档单价；未显式给峰价时按「谷价 × peak_multiplier」兜底（§9.2 峰谷设置）。"""
        prices = self.prices or {}
        if tier in prices and prices[tier]:
            return dict(prices[tier])
        base = dict(prices.get("off_peak") or {})
        if tier == "peak" and base:
            mult = float((self.schedule or {}).get("peak_multiplier") or 2.0)
            return {k: float(v) * mult for k, v in base.items()}
        return base


def resolve_quote(model: str = "", provider: str = "", base_url: str = "",
                  catalog: Optional[dict] = None) -> PriceQuote:
    """(model 别名, 供应商) → 价目。

    命中不了就给 billing='unknown' 的**空价目**：调用方据此记 null 并按策略告警，
    绝不猜一个价（猜价的成本数字比没有数字更糟）。
    """
    cat = catalog if catalog is not None else load_catalog()
    currency = str(_cfg("COST_CURRENCY", cat.get("currency") or "CNY") or "CNY")
    version = str(cat.get("version") or "")
    schedule = cat.get("peak_schedule") if isinstance(cat.get("peak_schedule"), dict) else {}

    host = _host_of(base_url)
    prov_name = str(provider or "").strip() or resolve_provider(model, base_url, cat)
    prov = _providers(cat).get(prov_name) if prov_name else None
    if not isinstance(prov, dict):
        # 展示上宁可给 host（可定位到具体中转），也不要给空串
        label = prov_name or host or "unknown"
        _warn_once("provider:" + label,
                   "[计价] 供应商无法识别（provider=%s host=%s）：成本记未计价",
                   prov_name or "<空>", host or "<空>")
        return PriceQuote(provider=label, model=normalize_model(model),
                          billing="unknown", schedule=schedule,
                          catalog_version=version, currency=currency)

    billing = billing_of(prov_name, prov, cat)
    models = prov.get("models") if isinstance(prov.get("models"), dict) else {}
    norm = normalize_model(model)
    key, entry = _match_model(models, model, norm)
    if entry is None:
        if billing == "paid":
            _warn_once("model:" + (norm or "-"), "[计价] 模型 %s 不在供应商 %s 的价目里，按未计价处理",
                       model or "<空>", prov_name)
            return PriceQuote(provider=prov_name, model=norm, billing="unknown",
                              schedule=schedule, catalog_version=version, currency=currency)
        return PriceQuote(provider=prov_name, model=norm, billing=billing,
                          schedule=schedule, catalog_version=version, currency=currency)

    prices: Dict[str, Dict[str, float]] = {}
    for tier in ("off_peak", "peak"):
        raw = entry.get(tier)
        if isinstance(raw, dict):
            prices[tier] = {k: float(raw.get(k) or 0.0)
                            for k in ("in_miss", "in_hit", "out")}
    if entry.get("free") is True and billing == "paid":
        billing = "free"
    return PriceQuote(provider=prov_name, model=key or norm, billing=billing, prices=prices,
                      schedule=schedule, catalog_version=version, currency=currency)


def _match_model(models: dict, raw_model: str,
                 norm: str) -> Tuple[str, Optional[dict]]:
    """先精确命中 key/别名，再退化到归一后比较（大小写/空格/版本后缀）。"""
    if not models:
        return "", None
    raw = str(raw_model or "").strip()
    for key, entry in models.items():
        names = [key] + list((entry or {}).get("aliases") or [])
        if raw and raw in names:
            return str(key), entry
    for key, entry in models.items():
        names = [key] + list((entry or {}).get("aliases") or [])
        if norm and norm in {normalize_model(n) for n in names}:
            return str(key), entry
    return "", None


def _warn_once(key: str, msg: str, *args) -> None:
    if key in _warned:
        return
    _warned.add(key)
    try:
        logger.warning(msg, *args)
    except Exception:
        pass


# ============================================================
# 计价
# ============================================================

@dataclass
class CostBreakdown:
    total: Optional[float] = None          # None = 未计价（unknown）
    input_miss: float = 0.0
    input_hit: float = 0.0
    output: float = 0.0
    cached_saved: float = 0.0              # 缓存命中省下的钱（对比全按未命中价）
    tier: str = "off_peak"
    provider: str = ""
    model: str = ""
    billing: str = "paid"
    currency: str = "CNY"
    catalog_version: str = ""
    price_snapshot: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """落库形态（steps.extra.cost，见设计文档 §6）。"""
        return {
            "total": self.total,
            "input_miss": round(self.input_miss, 8),
            "input_hit": round(self.input_hit, 8),
            "output": round(self.output, 8),
            "cached_saved": round(self.cached_saved, 8),
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "billing": self.billing,
            "currency": self.currency,
            "catalog_version": self.catalog_version,
            "price_snapshot": dict(self.price_snapshot or {}),
        }


def split_tokens(usage: Optional[dict]) -> Tuple[int, int, int]:
    """输入/缓存命中/输出 三段拆分。

    缓存命中按 cached_tokens 计（DeepSeek 自动前缀缓存），且**封顶输入**；
    供应商不返回缓存字段时全部按未命中（保守，宁高不低）。
    """
    usage = usage or {}
    try:
        inp = int(usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        inp = 0
    try:
        out = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        out = 0
    try:
        hit = int(usage.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        hit = 0
    hit = max(0, min(hit, inp))
    return max(0, inp), hit, max(0, out)


def calc_cost(usage: Optional[dict], quote: PriceQuote,
              ts: Optional[float] = None) -> CostBreakdown:
    """核心计价（纯函数，无副作用）。

    ⚠️ 思考 token 不额外加：utils/token_usage.py 已注明 reasoning_tokens **含在
    output_tokens 内**，再加一遍就是双计（设计文档 §11#2 的「实施前必查」）。
    """
    inp, hit, out = split_tokens(usage)
    miss = max(0, inp - hit)
    tier = "peak" if is_peak(ts, quote.schedule) else "off_peak"
    price = quote.price_of(tier)

    def _money(tokens: int, unit_price: float) -> float:
        return tokens / _UNIT * float(unit_price or 0.0)

    in_miss = _money(miss, price.get("in_miss", 0.0))
    in_hit = _money(hit, price.get("in_hit", 0.0))
    out_cost = _money(out, price.get("out", 0.0))
    saved = _money(hit, price.get("in_miss", 0.0)) - in_hit

    billing = str(quote.billing or "unknown")
    if billing == "paid" and not price:
        billing = "unknown"

    if billing == "free":
        total: Optional[float] = 0.0
    elif billing == "unknown":
        total = None
    else:
        total = in_miss + in_hit + out_cost

    return CostBreakdown(
        total=total, input_miss=in_miss, input_hit=in_hit, output=out_cost,
        cached_saved=max(0.0, saved), tier=tier, provider=quote.provider,
        model=quote.model, billing=billing, currency=quote.currency,
        catalog_version=quote.catalog_version, price_snapshot=price)


def compute_cost(usage: Optional[dict], model: str = "", base_url: str = "",
                 provider: str = "", ts: Optional[float] = None) -> Optional[CostBreakdown]:
    """采集点入口：一次调用 → 成本明细（总开关关闭时返回 None）。

    给 TokenRecorder / TraceRecorder 两处共用：同一个 ts、同一份价目 → 结果确定（§7）。
    """
    if not cost_enabled():
        return None
    try:
        quote = resolve_quote(model=model, provider=provider, base_url=base_url)
        cost = calc_cost(usage, quote, ts)
    except Exception:
        logger.debug("[计价] 成本计算失败（忽略）", exc_info=True)
        return None
    policy = str(_cfg("COST_UNKNOWN_PROVIDER_POLICY", "warn") or "warn").strip().lower()
    if cost.billing == "unknown" and policy == "zero":
        cost.total = 0.0
    return cost


def format_cost(cost: Optional[CostBreakdown], digits: int = 4) -> str:
    """人话展示：免费 / 未计价 / ¥0.0234(峰)。"""
    if cost is None:
        return "未计价"
    if cost.billing == "free":
        return "免费"
    if cost.total is None:
        return "未计价"
    tier = "峰" if cost.tier == "peak" else "谷"
    return "¥" + ("%.*f" % (digits, cost.total)) + "(" + tier + ")"


# ============================================================
# 按次计费的服务（DeepSeek Search / Fetch，2026-10-18）
# ============================================================

# 落库 JSON 文本里的「服务成本」标记，供聚合侧做 **SQL 预筛**（否则要全表扫 tool 行）。
# ⚠️ 必须与 `agent_trace/core.py::update_tool_step_cost` 的 `json.dumps` 默认分隔符
#    （`, ` / `: `）保持一致 —— 改分隔符就要同步这里。它只用于**降噪**：
#    Python 侧仍会再校验一次 `kind == "service"`，所以标记失配的后果是「扫多了」，
#    绝不会是「把服务成本漏掉」（那时 SQL 侧少筛、Python 侧照样能认出来）。
SERVICE_COST_JSON_MARKER = '"kind": "service"'

@dataclass
class ServiceCost:
    """按【请求次数】计费的服务成本，与按 token 计价的 LLM **并列但不同口径**。

    为什么不复用 `CostBreakdown`：后者的全部语义都建立在「tokens × 元/1M」和峰谷之上
    （见模块 docstring §5），按次费用塞进去只会得到一堆恒为 0 的 input/output 字段，
    而且极易被下游当成一次 LLM 调用，污染 `llm_calls`、缓存命中率分母与
    `avg_cost_per_call` —— 那是本项目明令禁止的口径混合。故落库形态自带 `kind='service'`，
    聚合侧「缺 kind 视为 llm」保证历史行语义不变。

    官方 Search/Fetch **不分峰谷**（统一按次价），所以这里没有 `tier`：
    套 `is_peak()` 只会凭空造出一个「谷时折扣」的假象。
    """
    service: str = ""                     # 价目 services 段的键，如 "deepseek-search"
    total: Optional[float] = None         # None = 未计价（价目里没有这个服务）
    count: int = 0                        # 计费请求次数
    unit_price: float = 0.0
    unit: str = "per_request"
    provider: str = ""
    billing: str = "paid"
    currency: str = "CNY"
    catalog_version: str = ""
    price_snapshot: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """落库形态（tool step 的 extra.cost）。`kind` 不可省——聚合侧靠它区分口径。"""
        return {
            "kind": "service",
            "service": self.service,
            "total": self.total,
            "count": self.count,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "provider": self.provider,
            "billing": self.billing,
            "currency": self.currency,
            "catalog_version": self.catalog_version,
            "price_snapshot": dict(self.price_snapshot or {}),
        }


def service_quote(service: str) -> ServiceCost:
    """按次价目查询（纯查表，不判峰谷）。缺失 → billing='unknown'（total 留给调用方置 None）。"""
    cat = load_catalog()
    name = str(service or "").strip()
    services = cat.get("services") if isinstance(cat.get("services"), dict) else {}
    entry = services.get(name)
    currency = str(cat.get("currency") or "CNY")
    version = str(cat.get("version") or "")
    if not isinstance(entry, dict):
        _warn_once("service:" + (name or "-"),
                   "[计价] 服务 %s 不在价目 services 段里，按未计价处理", name or "<空>")
        return ServiceCost(service=name, billing="unknown",
                           currency=currency, catalog_version=version)

    billing = str(entry.get("billing") or "paid").strip().lower()
    if entry.get("free") is True and billing == "paid":
        billing = "free"
    unit = str(entry.get("unit") or "per_request")
    price = float(entry.get("price") or 0.0)
    return ServiceCost(service=name, unit=unit, unit_price=price,
                       provider=str(entry.get("provider") or name), billing=billing,
                       currency=currency, catalog_version=version,
                       price_snapshot={"price": price, "unit": unit})


def calc_service_cost(service: str, count: int = 1) -> ServiceCost:
    """按次计价（纯函数）：`count × 单价`。

    三态与 LLM 侧严格一致：free → 0.0；unknown → **total=None**，绝不填 0
    （0 会被下游读成「算过了，不要钱」）。
    """
    quote = service_quote(service)
    try:
        n = max(0, int(count or 0))
    except (TypeError, ValueError):
        n = 0
    out = replace(quote, count=n)
    if quote.billing == "free":
        out.total = 0.0
    elif quote.billing == "unknown":
        out.total = None
    else:
        out.total = round(quote.unit_price * n, 8)
    return out


def compute_service_cost(service: str, count: int = 1) -> Optional[ServiceCost]:
    """采集点入口（总开关 `COST_TRACKING_ENABLED` 关闭时返回 None），与 `compute_cost` 同款。"""
    if not cost_enabled():
        return None
    try:
        cost = calc_service_cost(service, count)
    except Exception:
        logger.debug("[计价] 服务成本计算失败（忽略）", exc_info=True)
        return None
    policy = str(_cfg("COST_UNKNOWN_PROVIDER_POLICY", "warn") or "warn").strip().lower()
    if cost.billing == "unknown" and policy == "zero":
        cost.total = 0.0
    return cost


def format_service_cost(cost: Optional[ServiceCost], digits: int = 4) -> str:
    """人话展示：免费 / 未计价 / ¥0.0050。"""
    if cost is None:
        return "未计价"
    if cost.billing == "free":
        return "免费"
    if cost.total is None:
        return "未计价"
    return "¥" + ("%.*f" % (digits, cost.total))
