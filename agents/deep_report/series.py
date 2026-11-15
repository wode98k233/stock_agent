"""宽表 → 多期序列（M3）：把「日期在列名里」的工具返回转成带 `period` 的 items。

## 为什么需要这一层

`chart_candidates.build_candidates` 的 series 形状要求**同一 metric 有 ≥4 个不同
`period`**，才能产出折线候选；但取数工具默认返回的是「单期快照」，所以 series
候选长期恒为 0（长报告只有横截面柱图）。

而 `iwc_query` 的行情类问句返回的其实是**宽表**（日期在列名里，序列是列不是行）：

    data.columns = [{"key": "收盘价[20261030]", "unit": "点", "timestamp": "20261030"},
                    {"key": "收盘价[20261029]", "unit": "点", "timestamp": "20261029"}, ...]
    data.datas   = [{"指数简称": "中证银行", "收盘价[20261030]": 7962.2593, ...}]

本模块负责把它**转置**成 items 列表（每个 period 一条），供结论卡直接使用。

## 口径（都由 `scripts/devtools/probe_series_feasibility.py` 实测确认）

- 列名日期支持 `名称[YYYYMMDD]` 与 `名称[YYYY-MM-DD]`（括号也接受 `(YYYYMMDD)`），
  统一成 `YYYY-MM-DD`，输出按日期**升序**；
- `名称[区间]`（如 `区间成交额[20260928-20261030]`）是**区间合计单值**，不是序列，
  一律跳过 —— 这是「近20个交易日的成交额」拿不到折线的真因（要问「每日成交额」）；
- 数值清洗：去千分位/百分号/中文量级后缀（万亿/亿/万…），解析失败**跳过并计数**，
  绝不填 0、绝不猜；
- 一个 payload 可能同时含多组指标（收盘价 + 成交额 → 两组各 60 期），
  按 `metric_hint` 选一组返回；hint 不匹配时退化为「期数最多的那组」；
  一组序列天然同单位，混多组会让折线跨量级。

纯函数：无 IO、无 LLM、无网络，可直接单测。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 列名尾部日期：收盘价[20261030] / 收盘价[2026-10-30] / 收盘价(20261030)
_DATE_TAIL = re.compile(r"^(?P<name>.*?)[\[(]\s*(?P<y>\d{4})-?(?P<m>\d{2})-?(?P<d>\d{2})\s*[\])]\s*$")
# 长表里可能的日期列名
_DATE_FIELDS = ("date", "日期", "时间", "trade_date", "交易日期", "报告期")

# 「没有数」的各种写法：一律计为解析失败，不参与序列
_NULL_TOKENS = {
    "", "-", "--", "---", "—", "——", "－", "n/a", "na", "nan", "none", "null",
    "无", "暂无", "不适用", "空",
}
# 中文量级后缀 → 倍数（顺序有意义：先长后短，否则「万亿」会被「万」先吃掉）
_SCALE_SUFFIXES = (
    ("万亿", 1e12), ("亿", 1e8), ("千万", 1e7), ("百万", 1e6), ("万", 1e4),
)
# 指标名前缀噪音（个别问法会带出来）
_METRIC_PREFIX = re.compile(r"^(?:全量|固定)?(?:指数|板块|股票|行业|概念)@")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _norm_text(text: Any) -> str:
    """归一：去空格/全角空格，小写（用于 metric_hint 匹配）。"""
    return re.sub(r"[\s\u3000]+", "", str(text or "")).lower()


def _clean_metric(raw: str) -> str:
    """列名 → 指标名：去掉 `指数@` 之类前缀与尾部冒号。"""
    name = _METRIC_PREFIX.sub("", str(raw or "")).strip()
    return name.rstrip(":：").strip()


def _norm_period(year: str, month: str, day: str) -> Optional[str]:
    """(2026,10,30) → '2026-10-30'；非法日期返回 None（如 20261332）。"""
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def _clean_number(raw: Any) -> Optional[float]:
    """宽松转数值：去千分位/百分号/中文量级后缀；解析不出返回 None。

    - `None` / 空 / `—` / `N/A` / `nan` → None（调用方计为跳过）；
    - `"1,441.61"` → 1441.61；`"1.4%"` → 1.4（百分号是单位不是量级）；
    - `"2.3亿"` → 2.3e8、`"8336万"` → 8.336e7（同列混量级时才能画在同一根轴上）；
    - bool 视为脏数据（`True` 会变成 1.0，是静默错数）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        # NaN / ±inf 不是数（float('nan') 与自身不相等）
        return val if val == val and val not in (float("inf"), float("-inf")) else None

    text = str(raw).strip()
    # 全角数字/句点 → 半角（"１．５" 直接 float 会失败）；各种减号统一成 '-'
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    for dash in ("−", "－", "–", "—"):
        text = text.replace(dash, "-")
    text = text.replace(",", "").replace("，", "").replace(" ", "").replace("\u3000", "")
    if text.lower() in _NULL_TOKENS:
        return None
    text = text.replace("%", "").replace("％", "")

    # 会计负数写法 "(1.2)" / "(1.2)亿" = -1.2 / -1.2e8（不处理会被当成正数，静默改号）
    negative = "(" in text and ")" in text
    if negative:
        text = text.replace("(", "").replace(")", "")

    scale = 1.0
    for suffix, mult in _SCALE_SUFFIXES:
        if suffix in text:
            scale = mult
            text = text.replace(suffix, "")
            break

    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        val = float(match.group(0))
        if negative and val > 0:
            val = -val
        return val * scale
    except ValueError:      # pragma: no cover - 正则已保证可转
        return None


def _column_specs(columns: Any) -> List[Dict[str, str]]:
    """宽表列定义 → [{raw, name, unit}]。

    `columns` 有两种形态（都实测存在）：
    - 元数据字典：`{"key": "收盘价[20261030]", "unit": "点", "timestamp": "20261030"}`
    - 纯列名：`"收盘价[20261030]"`
    """
    out: List[Dict[str, str]] = []
    for col in columns or []:
        if isinstance(col, dict):
            raw = str(col.get("key") or col.get("feKey") or col.get("index_name") or "")
            unit = str(col.get("unit") or "")
        else:
            raw, unit = str(col or ""), ""
        out.append({"raw": raw, "unit": unit})
    return out


def _dated_columns(columns: Any, stats: Optional[Dict[str, Any]] = None
                   ) -> List[Tuple[int, str, str, str]]:
    """挑出「列名带日期」的列 → [(列下标, 指标名, 日期, 单位)]。

    日期看着像但日历上不存在的列（如 `[20261332]`）计进 `skipped_columns`：
    静默丢掉这类列等于悄悄少画几个点，必须留痕。
    """
    found: List[Tuple[int, str, str, str]] = []
    for idx, spec in enumerate(_column_specs(columns)):
        raw = spec["raw"]
        if not raw:
            continue
        m = _DATE_TAIL.match(raw)
        if not m:                       # 区间列 / 无日期列 → 不是序列
            continue
        period = _norm_period(m.group("y"), m.group("m"), m.group("d"))
        metric = _clean_metric(m.group("name"))
        if not period or not metric:
            if stats is not None:
                stats["skipped_columns"] = stats.get("skipped_columns", 0) + 1
            continue
        found.append((idx, metric, period, spec["unit"]))
    return found


def _row_values(row: Any, columns: List[Dict[str, str]]) -> List[Any]:
    """一行 → 按列下标对齐的值列表（行是 dict 按列名取，是 list 按位置取）。"""
    if isinstance(row, dict):
        return [row.get(spec["raw"]) for spec in columns]
    if isinstance(row, (list, tuple)):
        return list(row)
    return []


def _best_row(datas: Any, dated: List[Tuple[int, str, str, str]],
              columns: List[Dict[str, str]]) -> Optional[List[Any]]:
    """从多行里挑「有效数值最多」的一行。

    宽表每行是一个标的（实测「银行板块」类问句会返回 3 行：881155.TI 等），
    同一张折线只能画一个标的，所以按可解析数值数量取最优行（并列取最靠前）。
    """
    best: Optional[List[Any]] = None
    best_n = -1
    for row in datas or []:
        values = _row_values(row, columns)
        if not values:
            continue
        n = sum(1 for idx, *_ in dated
                if idx < len(values) and _clean_number(values[idx]) is not None)
        if n > best_n:
            best, best_n = values, n
    return best


def _families_from_wide(obj: Dict[str, Any], stats: Dict[str, Any]
                        ) -> Dict[str, Tuple[str, Dict[str, float]]]:
    """宽表 → {指标名: (单位, {period: value})}。"""
    columns_raw = obj.get("columns")
    datas = obj.get("datas")
    if not isinstance(columns_raw, list) or not isinstance(datas, list):
        return {}
    columns = _column_specs(columns_raw)
    stats["columns"] = stats.get("columns", 0) + len(columns)
    dated = _dated_columns(columns_raw, stats)
    stats["dated_columns"] = stats.get("dated_columns", 0) + len(dated)
    if not dated:
        return {}

    row = _best_row(datas, dated, columns)
    if row is None:
        return {}
    stats["rows"] = stats.get("rows", 0) + 1

    families: Dict[str, Tuple[str, Dict[str, float]]] = {}
    for idx, metric, period, unit in dated:
        raw = row[idx] if idx < len(row) else None
        value = _clean_number(raw)
        if value is None:
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        unit_of, points = families.setdefault(metric, (unit, {}))
        points[period] = value
        if not unit_of and unit:
            families[metric] = (unit, points)
    return families


def _find_date_field(fieldnames: List[str]) -> Optional[str]:
    for name in fieldnames:
        low = str(name).strip().lower()
        if low in _DATE_FIELDS or "date" in low:
            return name
    return None


def _families_from_long(obj: Dict[str, Any], stats: Dict[str, Any]
                        ) -> Dict[str, Tuple[str, Dict[str, float]]]:
    """长表（一行一日期）→ {指标名: (单位, {period: value})}。

    `mx_data_query` 的 `tables` 是这个形态（`agents/analysis` 侧早有对应解析）：
        fieldnames = ["date", "收盘价"]，rows = [{"date": "2026-10-30", "收盘价": "7962.26"}]
    ⚠️ 该形态未实测（探针当天妙想额度耗尽），按 `mx_data.table_to_rows` 的代码口径实现；
    解析不出就返回空，调用方降级为单期行为。
    """
    tables = obj.get("tables")
    if not isinstance(tables, list):
        return {}
    families: Dict[str, Tuple[str, Dict[str, float]]] = {}
    for table in tables:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows") or []
        fieldnames = [str(f) for f in (table.get("fieldnames") or [])]
        if not rows or not fieldnames:
            continue
        date_field = _find_date_field(fieldnames)
        if date_field is None:
            continue
        stats["rows"] = stats.get("rows", 0) + len(rows)
        labels = [f for f in fieldnames if f != date_field]
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_period = str(row.get(date_field) or "").strip()
            m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", raw_period)
            period = _norm_period(m.group(1), m.group(2), m.group(3)) if m else None
            if not period:
                continue
            for label in labels:
                metric = _clean_metric(label)
                if not metric:
                    continue
                value = _clean_number(row.get(label))
                if value is None:
                    stats["skipped"] = stats.get("skipped", 0) + 1
                    continue
                _unit, points = families.setdefault(metric, ("", {}))
                points[period] = value
    return families


def _guess_source(obj: Any) -> str:
    """从 payload 形态猜来源名（结论卡 items 必须带 source，不能为空）。"""
    if isinstance(obj, dict):
        if "tables" in obj or "terminal_output" in obj:
            return "mx_data_query"
        if "skill_id" in obj or "datas" in obj:
            return "iwc_query"
    return "wide_table"


def _pick_family(families: Dict[str, Tuple[str, Dict[str, float]]],
                 hint: str) -> Optional[Tuple[str, str, Dict[str, float]]]:
    """选一组序列：hint 命中优先（完全相等 > hint 是子串 > 指标名是 hint 子串），
    否则取期数最多的那组（并列按名称排序，保证可复现）。"""
    if not families:
        return None
    key = _norm_text(hint)
    if key:
        best: Optional[Tuple[int, str]] = None
        for name in families:
            norm = _norm_text(name)
            if not norm:
                continue
            if norm == key:
                score = 100
            elif key in norm:
                score = 50 + len(norm)
            elif norm in key:
                score = 40 + len(norm)
            else:
                continue
            if best is None or score > best[0]:
                best = (score, name)
        if best is not None:
            unit, points = families[best[1]]
            return best[1], unit, points
    name = max(sorted(families), key=lambda k: len(families[k][1]))
    unit, points = families[name]
    return name, unit, points


def parse_wide_series(payload_text: Any, metric_hint: str = "",
                      *, source: str = "", stats: Optional[Dict[str, Any]] = None
                      ) -> List[Dict[str, Any]]:
    """工具返回的宽表 → 同一指标的多期 items（按 period 升序）。

    :param payload_text: 工具返回的 JSON 文本（也接受已解析的 dict）
    :param metric_hint: 想要的指标名（如「收盘价」）：多指标 payload 里选哪一组
    :param source: 覆盖 items 里的来源名；缺省按 payload 形态推断
    :param stats: 传一个 dict 收集计数（columns/dates/skipped…），便于日志与断言
    :return: [{"metric","value","unit","source","period"}]；解析不出返回 []

    解析失败（无日期列 / JSON 非法 / 数值全是 `—`）一律返回**空列表**，由调用方
    降级为原有单期行为；本函数不抛异常。
    """
    st: Dict[str, Any] = stats if stats is not None else {}
    # 计数先全部置 0：调用方（fetch 日志 / 单测断言）不必区分「没发生」与「没这个键」
    for key in ("columns", "dated_columns", "rows", "skipped", "skipped_columns",
                "families"):
        st.setdefault(key, 0)

    obj: Any = payload_text
    if isinstance(payload_text, (str, bytes)):
        try:
            obj = json.loads(payload_text)
        except (ValueError, TypeError):
            st["error"] = "invalid_json"
            return []
    if not isinstance(obj, dict):
        st["error"] = "not_object"
        return []

    families: Dict[str, Tuple[str, Dict[str, float]]] = {}
    # iwc 的宽表可能整体包在 data 里，也可能平铺（两种都见过）
    blocks = [obj]
    inner = obj.get("data")
    if isinstance(inner, dict):
        blocks.append(inner)
    for block in blocks:
        for name, (unit, points) in _families_from_wide(block, st).items():
            merged_unit, merged = families.setdefault(name, (unit, {}))
            merged.update(points)
            if not merged_unit and unit:
                families[name] = (unit, merged)
    if not families:                    # 宽表没有 → 试长表（mx 口径）
        families = _families_from_long(obj, st)

    st["families"] = len(families)
    picked = _pick_family(families, metric_hint)
    if picked is None:
        logger.debug("宽表无序列可解析（hint=%r）: %s", metric_hint, st)
        return []
    metric, unit, points = picked
    st["picked"] = metric
    st["periods"] = len(points)

    src = str(source or "").strip() or _guess_source(obj)
    items: List[Dict[str, Any]] = []
    for period in sorted(points):
        items.append({"metric": metric, "value": points[period],
                      "unit": unit, "source": src, "period": period})
    return items
