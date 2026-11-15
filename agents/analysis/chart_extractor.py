"""图表片段萃取器（Track 1 规则萃取）— 对应 docs/superpowers/specs/2026-07-06-chart-fragments-design.md

从 mx_data 工具返回的结构化 tables（fieldnames + rows）自动生成 ECharts-ready 图表数据。
已接入 agent 主流程：agents/react/tool_node_base.py 在 mx_data_query 执行后拦截调用
extract_from_tool_output，生成 @@CHART:uuid@@ 标记由前端渲染；chart_debug CLI 用于
独立调试与历史 fragment 核对。

真实列名校准（2026-08 实测）：
  - 均线: MA5 / MA10 / MA20 / MA60
  - 指标: RSI相对强弱指标 / MACD柱状线 / MACD指数平滑异同平均-DIFF / MACD指数平滑异同平均-DEA
  - 财务: 营业总收入 / 净利润 / 归属母公司股东的净利润 / 净资产收益率ROE(加权)
  - 资金: 主力净流入资金 / 超大单净流入资金 / 大单净流入资金
"""
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Optional

# ── 规则模式：keys 任一命中即触发，同 pattern 命中的列组成一个图表（多系列） ──
# 列名按 2026-08 真实 mx_data 返回校准：
#   均线: '10日MA简单移动平均' / '5日MA简单移动平均' 等
#   RSI: 'RSI相对强弱指标'；MACD: 'MACD柱状线' / 'MACD指数平滑异同平均-DIFF' 等
_PATTERNS = [
    {
        "keys": ("简单移动平均", "日MA"),
        "chart_type": "time_series",
        "title": "均线",
        "series_type": "line",
    },
    {
        "keys": ("RSI",),
        "chart_type": "indicator",
        "title": "RSI",
        "series_type": "line",
    },
    {
        "keys": ("MACD",),
        "chart_type": "indicator",
        "title": "MACD",
        "series_type": "line",
    },
    {
        "keys": ("营收", "净利润", "ROE", "毛利率"),
        "chart_type": "comparison",
        "title": "财务",
        "series_type": "bar",
    },
    {
        "keys": ("净流入",),
        "chart_type": "comparison",
        "title": "资金流向",
        "series_type": "bar",
    },
]

# 单图表最多系列数，防止一个财务表命中几十列
_MAX_SERIES = 4


def _to_float(value: Any) -> Optional[float]:
    """宽松转数值：兼容 %、亿/万等单位后缀和千分位，单位统一换算到基准值。

    mx_data 同一列的数值按量级自动格式化（'2.308亿' vs '8336万'），若只取数字
    丢单位，混量级会让小值在柱状图上变成亚像素柱（有值但看不见）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    num = float(m.group(0))
    if "%" in text:
        return num
    suffix = text[m.end():]
    if "万亿" in suffix:
        return num * 1e12
    if "亿" in suffix:
        return num * 1e8
    if "千万" in suffix:
        return num * 1e7
    if "百万" in suffix:
        return num * 1e6
    if "万" in suffix:
        return num * 1e4
    return num


def _short_col_name(col: str) -> str:
    """简化长中文列名为图表系列名：
    - '10日MA简单移动平均' -> 'MA10'（'\\\\d+日MA' 优先）
    - 含括号取括号内（'市盈率PE(TTM)' -> 'PE(TTM)'）
    - 含 '-' 取 '-' 后（'MACD指数平滑异同平均-DIFF' -> 'DIFF'）
    - 'RSI相对强弱指标' 等前缀长名 -> 提取首个字母词（'RSI'）
    - 否则原样
    """
    m = re.search(r"(\d+)日MA", col)
    if m:
        return f"MA{m.group(1)}"
    m = re.search(r"\(([^)]+)\)", col)
    if m:
        return m.group(1)
    if "-" in col:
        return col.split("-")[-1]
    m = re.match(r"([A-Za-z0-9]+)", col)
    if m:
        return m.group(1)
    return col


# 指标名归一：问财的列名带着域前缀与口径后缀（'净资产收益率roe(加权,公布值)同比增长率'），
# 直接当图例会把 ECharts 图例撑破，也读不出是什么指标。
_IWC_METRIC_ALIASES = (
    ("净资产收益率roe", "ROE"),
    ("净资产收益率", "ROE"),
    ("归属于母公司所有者的净利润", "净利润"),
    ("归属于母公司股东的净利润", "净利润"),
    ("归母净利润", "净利润"),
    ("营业收入(同比增长率)", "营收同比"),
    ("营业收入", "营收"),
    ("市盈率", "PE"),
    ("市净率", "PB"),
    ("涨跌幅:前复权", "涨跌幅"),
    ("涨跌幅:不复权", "涨跌幅"),
    ("收盘价:前复权", "收盘价"),
    ("收盘价:不复权", "收盘价"),
)


def _short_metric_name(base: str) -> str:
    """把问财列名压成可读图例：'净资产收益率roe(加权,公布值)同比增长率' → 'ROE同比'。"""
    name = str(base or "")
    for old, new in _IWC_METRIC_ALIASES:
        name = name.replace(old, new)
    for junk in ("(加权,公布值)", "（加权,公布值）", "(pe,ttm)", "(PE,TTM)"):
        name = name.replace(junk, "")
    name = name.replace("同比增长率", "同比").replace("环比增长", "环比")
    name = name.replace("最新", "").replace("ttm", "TTM")
    return name.strip() or str(base or "")


def _find_date_col(fieldnames: list) -> str:
    """找日期列：优先含 date，否则第一列。"""
    for f in fieldnames:
        if "date" in str(f).lower() or f in ("日期", "时间"):
            return f
    return fieldnames[0] if fieldnames else ""


def _match_patterns(fieldnames: list) -> list:
    """对 fieldnames 做规则匹配，返回 [(pattern, [匹配列]), ...]。"""
    hits = []
    for pat in _PATTERNS:
        matched = [f for f in fieldnames if any(k in str(f) for k in pat["keys"])]
        if matched:
            hits.append((pat, matched[: _MAX_SERIES]))
    return hits


def _build_data_json(pat: dict, fieldnames: list, rows: list, matched_cols: list) -> Optional[dict]:
    """从表格行提取系列数据，构造 ECharts-ready JSON。"""
    if not rows:
        return None
    date_col = _find_date_col(fieldnames)
    x_data = []
    for row in rows:
        d = row.get(date_col) if isinstance(row, dict) else None
        x_data.append(str(d) if d is not None else "")
    if not any(x_data):
        return None
    # 单点快照表（'当前的RSI值' 等）画图无意义，跳过
    if len(x_data) < 2:
        return None

    series = []
    for col in matched_cols:
        data = []
        for row in rows:
            v = row.get(col) if isinstance(row, dict) else None
            data.append(_to_float(v))
        if all(v is None for v in data):
            continue
        series.append({
            "name": _short_col_name(col),
            "type": pat["series_type"],
            "data": data,
        })
    if not series:
        return None

    option = {
        "xAxis": [{"data": x_data}],
        "series": series,
    }
    # indicator 类型附加参考线（RSI 超买超卖 / MACD 零轴）
    if pat["chart_type"] == "indicator":
        if "RSI" in pat["title"]:
            option["markLine"] = [
                {"yAxis": 70, "label": "超买"},
                {"yAxis": 30, "label": "超卖"},
            ]
        elif "MACD" in pat["title"]:
            option["markLine"] = [{"yAxis": 0, "label": "零轴"}]
    return option


# sheet_name 实体提取：'药明康德(603259.SH)的RSI...' -> ('药明康德', '603259')
# 支持 A 股 6 位 / 港股 5 位代码
_ENTITY_RE = re.compile(r"([^()（）]+?)\((\d{5,6})")


def _extract_entity(table: dict) -> tuple[str, str]:
    """从表格 sheet_name 提取真实数据实体（名称+代码）。

    一个查询可能返回多只股票的表格（如创新药对比），每个表格的
    sheet_name 自带真实实体，必须按表级提取，不能用查询里的代码。
    """
    sheet = str(table.get("sheet_name", ""))
    m = _ENTITY_RE.search(sheet)
    if m:
        return m.group(1).strip(), m.group(2)
    return "", ""


# ═══════════════════════════════════════════════════════════════
# mx_data_query 表格的四种「朝向」
#
# 2026-11-03 实测：妙想返回的 tables 并不是只有「一行一日期、一列一指标」这一种。
# 同一次 run 里四种朝向都出现过，而旧实现只认第 ④ 种，于是报告里零图：
#
#   ① 横表·列是标的：rows=1，fieldnames=[date, 上证指数(…), 深证成指(…), …]
#   ② 截面·行是标的：fieldnames=[date, 成交额, 涨跌幅]，date 列里装的是「上证指数」
#   ③ 时序矩阵：rows=日期，列=标的（白酒Ⅱ(板块) / 食品饮料(板块)）
#   ④ 长表·行是日期：rows=日期，列=指标（均线/RSI/MACD/财务/资金流）← 旧实现唯一支持
#
# 判别只靠两件事：date 列的值像不像日期、列名像不像标的。判错会画出
# 「拿指标当标的」的怪图（2026-11-03 用户截图里的「板块累计涨跌幅 vs 上证指数」）。
# ═══════════════════════════════════════════════════════════════

_DATE_LIKE_RE = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}")
# 标的形态：带代码括号，或者板块/指数/ETF 这类名字
_ENTITY_HINT_RE = re.compile(
    r"[（(]\d{5,6}(?:\.[A-Za-z]{2})?[）)]|板块|指数|ETF|LOF|基金|行业|证券")

# 单指标折线的指标优先级（长表里没有均线/RSI 这类规则命中时用它兜底）
_SERIES_METRICS = ("收盘价", "最新价", "收盘点位", "成交额", "成交金额", "涨跌幅")
_CROSS_METRICS = ("涨跌幅", "最新涨跌幅", "主力净流入", "净流入", "成交额",
                  "收盘价", "最新价")


def _looks_date_like(value) -> bool:
    return bool(_DATE_LIKE_RE.search(str(value or "")))


def _looks_entity_name(value) -> bool:
    return bool(_ENTITY_HINT_RE.search(str(value or "")))


def _short_entity(field: str) -> str:
    """列名里的标的显示名：'上证指数(000001.SH)(指数)' → '上证指数'。"""
    name = re.sub(r"[（(]\d{5,6}(?:\.[A-Za-z]{2})?[）)]", "", str(field or ""))
    name = name.replace("(指数)", "").replace("(板块)", "").strip()
    return name or str(field or "")


def _metric_from_sheet(sheet_name: str) -> str:
    """从 sheet 名里抠指标名：'食品饮料(板块)等的(区间)主力净流入资金' → '主力净流入资金'。"""
    text = str(sheet_name or "")
    for sep in ("等当前的", "等的", "的"):
        if sep in text:
            text = text.split(sep, 1)[1]
            break
    text = text.split("、")[0]
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip()
    return text[:14] or "数值"


def _pick_metric_field(fields: list, priority) -> str:
    """按优先级挑一个指标列（同关键词取列名最短的，避开「xxx同比增长率」这类变体）。"""
    for kw in priority:
        cands = sorted([f for f in fields if kw.lower() in str(f).lower()],
                       key=lambda x: (len(str(x)), str(x)))
        for f in cands:
            if any(x in str(f) for x in ("增长", "同比", "环比", "累计", "区间")):
                continue
            return f
    return ""


def _table_cross_fragment(rows: list, fields: list, entity_values: list,
                          metric_field: str, unit: str, title_prefix: str,
                          code: str, name: str, dialog_uuid: str) -> list:
    """行=标的的截面柱状图（② 与 ① 共用）。"""
    if not metric_field or len(entity_values) < 2:
        return []
    data = [_to_float(r.get(metric_field)) for r in rows]
    if all(v is None for v in data):
        return []
    metric = _short_metric_name(metric_field)
    first = next((v for v in entity_values if v), "")
    n = len([v for v in entity_values if v])
    scope = f"{first}等{n}只" if n > 1 else first
    option = {
        "xAxis": [{"data": entity_values}],
        "series": [{"name": metric, "type": "bar", "data": data}],
    }
    if unit:
        option["yUnit"] = unit
    return [_mk_fragment("comparison", code, name, f"{metric}对比·{scope}",
                         option, dialog_uuid)]


def _table_series_fragment(rows: list, fields: list, date_col: str, entities: list,
                           dialog_uuid: str, code: str = "", name: str = "",
                           metric_hint: str = "") -> list:
    """③ 时序矩阵：行=日期、列=标的 → 多线折线图。"""
    if len(rows) < 3 or not entities:
        return []
    ordered = sorted(
        [r for r in rows if _looks_date_like(r.get(date_col))],
        key=lambda r: str(r.get(date_col)),
    )[-_SERIES_MAX_POINTS:]
    if len(ordered) < 3:
        return []
    dates = [str(r.get(date_col)) for r in ordered]
    series = []
    for f in entities[:_SERIES_MAX_ROWS_SIMPLE]:
        data = [_to_float(r.get(f)) for r in ordered]
        if all(v is None for v in data):
            continue
        series.append({"name": _short_entity(f), "type": "line", "data": data})
    if not series:
        return []
    metric = _short_metric_name(
        next((kw for kw in _SERIES_METRICS if kw in str(fields)), "") or metric_hint or "数值")
    option = {"xAxis": [{"data": dates}], "series": series}
    return [_mk_fragment("time_series", code, name, f"{metric}走势", option, dialog_uuid)]


def _mk_fragment(chart_type: str, code: str, name: str, title: str,
                 option: dict, dialog_uuid: str = "") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "dialog_uuid": dialog_uuid,
        "chart_type": chart_type,
        "stock_code": code,
        "stock_name": name,
        "title": title.strip(),
        "data_json": json.dumps(option, ensure_ascii=False),
        "annotations": "[]",
        "source": "rule",
    }


_SERIES_MAX_ROWS_SIMPLE = 4


def _table_fragments(table: dict, stock_code: str, stock_name: str,
                     dialog_uuid: str) -> list:
    """单表 → 图表（按朝向分派，最多 2 张）。"""
    fieldnames = table.get("fieldnames") or []
    rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
    if not fieldnames or not rows:
        return []
    entity_name, entity_code = _extract_entity(table)
    code = entity_code or stock_code
    name = entity_name or stock_name
    sheet = str(table.get("sheet_name") or "")
    date_col = _find_date_col(fieldnames)
    others = [f for f in fieldnames if f != date_col]
    if not others:
        return []

    # ① 单行 + 列是标的 → 横表截面
    entity_cols = [f for f in others if _looks_entity_name(f)]
    if len(rows) == 1 and len(entity_cols) >= 2:
        metric = _metric_from_sheet(sheet)
        labels = [_short_entity(f) for f in entity_cols]
        data = [_to_float(rows[0].get(f)) for f in entity_cols]
        if any(v is not None for v in data):
            scope = f"{labels[0]}等{len(labels)}只" if len(labels) > 1 else labels[0]
            return [_mk_fragment(
                "comparison", code, name, f"{metric}对比·{scope}",
                {"xAxis": [{"data": labels}],
                 "series": [{"name": metric, "type": "bar", "data": data}]},
                dialog_uuid)]

    col_values = [rows[i].get(date_col) for i in range(len(rows))]
    dateish = sum(1 for v in col_values if _looks_date_like(v))

    # ② date 列里装的是标的 → 截面柱状
    if dateish == 0 and len(rows) >= 2:
        labels = [_short_entity(v) for v in col_values]
        metric = _pick_metric_field(others, _CROSS_METRICS)
        return _table_cross_fragment(
            rows, fieldnames, labels, metric, "",
            f"{name or _metric_from_sheet(sheet)}", code, name, dialog_uuid)

    # ③ 行=日期、列=标的 → 多线折线
    if dateish >= 2 and len(entity_cols) >= 2:
        frags = _table_series_fragment(rows, fieldnames, date_col, entity_cols,
                                       dialog_uuid, code, name,
                                       metric_hint=_metric_from_sheet(sheet))
        if frags:
            return frags

    # ④ 长表：原有规则（均线/RSI/MACD/财务/资金流）
    out = []
    for pat, matched_cols in _match_patterns(fieldnames):
        data_json = _build_data_json(pat, fieldnames, rows, matched_cols)
        if data_json is None:
            continue
        out.append(_mk_fragment(pat["chart_type"], code, name,
                                f"{name or code} {pat['title']}".strip(),
                                data_json, dialog_uuid))
    if out:
        return out[:2]

    # ⑤ 长表兜底：单指标折线（收盘价/成交额/涨跌幅）
    if dateish >= 3:
        metric = _pick_metric_field(others, _SERIES_METRICS)
        if metric:
            ordered = sorted(
                [r for r in rows if _looks_date_like(r.get(date_col))],
                key=lambda r: str(r.get(date_col)),
            )[-120:]
            dates = [str(r.get(date_col)) for r in ordered]
            data = [_to_float(r.get(metric)) for r in ordered]
            if any(v is not None for v in data):
                return [_mk_fragment(
                    "time_series", code, name,
                    f"{name or code} {_short_metric_name(metric)}",
                    {"xAxis": [{"data": dates}],
                     "series": [{"name": _short_metric_name(metric),
                                 "type": "line", "data": data}]},
                    dialog_uuid)]
    return []


def _merge_series_tables(tables: list, dialog_uuid: str):
    """把「同结构、每张表一个标的」的长表合并成一张多线图。

    典型来源（2026-11-03 实测）：问「上证指数 深证成指 创业板指 科创50 最新点位」，
    妙想会返回 4 张同构表，每张 `fields=['date','成交额','收盘价','涨跌幅']`、
    sheet 名里各带一个指数。逐表画会得到 4 张几乎一样的单线图，合并成一张
    4 条线的走势图才有信息量。

    返回 (fragments, 已消费的 table id 集合)。
    """
    groups: dict = {}
    for table in tables:
        fieldnames = tuple(table.get("fieldnames") or [])
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        if not fieldnames or len(rows) < 3:
            continue
        date_col = _find_date_col(list(fieldnames))
        if sum(1 for r in rows if _looks_date_like(r.get(date_col))) < 3:
            continue
        entity_name, _ = _extract_entity(table)
        if not entity_name:
            continue
        groups.setdefault((fieldnames, date_col), []).append((entity_name, rows, id(table)))

    out, consumed = [], set()
    for (fieldnames, date_col), items in groups.items():
        if len(items) < 2:
            continue
        metric = _pick_metric_field([f for f in fieldnames if f != date_col], _SERIES_METRICS)
        if not metric:
            continue
        day_map: dict = {}
        for entity_name, rows, _tid in items:
            for r in rows:
                day = str(r.get(date_col))
                day_map.setdefault(day, {})[entity_name] = _to_float(r.get(metric))
        days = sorted(day_map)[-_SERIES_MAX_POINTS:]
        if len(days) < 3:
            continue
        series = []
        for entity_name, _rows, _tid in items[:_SERIES_MAX_ROWS_SIMPLE]:
            values = [(day_map.get(d) or {}).get(entity_name) for d in days]
            if all(v is None for v in values):
                continue
            series.append({"name": entity_name, "type": "line", "data": values})
        if len(series) < 2:
            continue
        out.append(_mk_fragment(
            "time_series", "", "", f"{_short_metric_name(metric)}走势",
            {"xAxis": [{"data": days}], "series": series}, dialog_uuid))
        consumed.update(tid for _, _, tid in items)
    return out, consumed


def _dedupe_fragments(fragments: list, keep: int = 4) -> list:
    """同一次工具调用内的图表去重：同一 (chart_type, 指标) 只留数据点最多的那张。

    不做这一步的话，一次查询会产出「4 个指数」「2 个指数」等多张同指标图，
    报告尾部变成同一件事的复读机。
    """
    best: dict = {}
    order: list = []
    for frag in fragments:
        try:
            option = json.loads(frag["data_json"])
            points = len((option.get("xAxis") or [{}])[0].get("data") or [])
            series = option.get("series") or [{}]
            key = (frag["chart_type"], str(series[0].get("name") or frag["title"]))
        except Exception:
            points, key = 0, (frag["chart_type"], frag["title"])
        if key not in best:
            order.append(key)
            best[key] = (points, frag)
        elif points > best[key][0]:
            best[key] = (points, frag)
    return [best[k][1] for k in order][:keep]


def extract_from_tables(tables: list, stock_code: str = "", stock_name: str = "",
                        dialog_uuid: str = "") -> list[dict]:
    """规则萃取主入口：扫描 tables → 生成 ChartFragment dict 列表（未入库）。

    tables: mx_data 返回的 [{"sheet_name", "fieldnames", "rows"}, ...]
    实体标注：优先取每个表格 sheet_name 里的真实实体（多股查询各表独立），
    提取不到时回退到调用方传入的 stock_code/stock_name。
    四种表朝向的分派见 `_table_fragments`；同构多表先合并成多线图。
    """
    tables = [t for t in tables if isinstance(t, dict)]
    merged, consumed = _merge_series_tables(tables, dialog_uuid)
    fragments = list(merged)
    for table in tables:
        if id(table) in consumed:
            continue
        try:
            fragments.extend(_table_fragments(table, stock_code, stock_name, dialog_uuid))
        except Exception:
            continue
    return _dedupe_fragments(fragments)


# ═══════════════════════════════════════════════════════════════
# 问财（iwc_query）萃取：截面 → 对比柱状图；宽表 → 折线图
#
# 为什么必须有这一层（2026-11-02）：本项目的图表曾被硬编码成「只认
# mx_data_query 的 tables 结构」，而 agent 的主力数据源早已换成 iwc_query
# （同花顺问财，见 tools/other_skills/10jqka/skills.py）。两者返回结构完全不同：
#   mx_data_query: {"tables": [{"sheet_name", "fieldnames", "rows"}]}  ← 长表
#   iwc_query:     {"data": {"columns": [...], "datas": [...]}}        ← 宽表
# 结果 chart_fragments 从 2026-10-30 起再无新增，报告全零图（连柱状/折线都没有）。
#
# 问财数据的两个形态必须分开处理：
#   1) **截面**（48/49 次调用）：datas 一行一标的、columns 是指标 → x 轴用标的名，
#      图表是「多标的对比柱状图」；
#   2) **宽表时间序列**：同一指标带一串日期后缀列（收盘价[20261022]…×120）→
#      x 轴用日期，图表是折线。日期在**列名**里，不在行里，这是与 mx 最大的差异。
#
# ⚠️ 区间合计列必须整列丢弃：`涨跌幅[20260929-20261102]` 是区间累计值，
# 与 `涨跌幅[20261102]`（当日）混在一张图上会让读者误读（见
# agents/deep_report/series.py 的同款判定与 test_interval_aggregate_rejected）。
# ═══════════════════════════════════════════════════════════════

# 支持图表萃取的工具有哪些（tool_node_base 与 extract_from_tool_output 共用一份，
# 避免两处白名单各写各的——历史上就是两处硬编码，谁先改谁静默失效）
CHART_TOOLS = frozenset({"mx_data_query", "iwc_query", "get_etf_history"})

# 返回 OHLCV 序列（可以直接画折线）的工具
_BAR_TOOLS = frozenset({"get_etf_history"})

_IWC_TS_SUFFIX_RE = re.compile(r"[\[(]([^\])\s]+)[\])]\s*$")
_IWC_RANGE_TS_RE = re.compile(r"\d{4}-?\d{2}-?\d{2}\s*[-~至]\s*\d{4}-?\d{2}-?\d{2}")
_IWC_DAY_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")
_IWC_NUMERIC_TYPES = ("DOUBLE", "FLOAT", "INT", "INTEGER", "LONG", "NUMBER")
_IWC_NAME_LABELS = ("name",)
_IWC_CODE_LABELS = ("code",)
_IWC_NAME_KEYS = ("股票简称", "指数简称", "基金简称", "板块名称", "名称", "简称")
_IWC_CODE_KEYS = ("股票代码", "指数代码", "基金代码", "板块代码", "代码")

_SERIES_MAX_POINTS = 120      # 折线最多 120 个点（与 deep_report 的 SERIES_MAX_POINTS 对齐）
_SERIES_MAX_ROWS = 4          # 宽表里最多画 4 个标的的线
_SERIES_MAX_ROWS_SIMPLE = 4   # mx 时序矩阵里最多画 4 个标的的线
_CROSS_MAX_ROWS = 20          # 截面最多画 20 根柱
_CROSS_MAX_CHARTS = 3         # 单次工具输出最多产出 3 张对比图（防图表墙）

# 截面图规则：keys 按优先级**逐个尝试**，先命中先选中（每张图只画 1 个指标）。
#
# 为什么要「优先级 + 单指标」而不是「关键词全收」（2026-11-02 实测教训）：
# 问财一份截面里同一业务概念会有很多列 —— 最新涨跌幅 / 涨跌幅[当日] /
# 涨跌幅:前复权 / 涨跌幅:前复权同比增长率 / 涨跌幅[20日区间累计]，关键词都是
# 「涨跌幅」。全收会把「当日涨跌幅」和「同比增长率（实测有 +156%、-756%）」
# 画进同一个坐标轴，读者直接误判。同理 PE 与 PB 单位不同，绝不能同轴。
_CROSS_PATTERNS = [
    {
        "keys": ("最新涨跌幅", "涨跌幅", "涨跌"),
        "title": "涨跌幅对比",
        "label": "涨跌幅",
        # 增长率/区间累计/振幅换手都不是「当日涨跌幅」，一律不选
        "exclude": ("增长", "同比", "环比", "累计", "区间", "振幅", "换手", "分位"),
    },
    {
        "keys": ("市盈率", "pe", "市净率", "pb"),
        "title": "估值对比",
        "label": "市盈率TTM",
        "exclude": ("增长", "分位", "百分位", "历史"),
    },
    {
        "keys": ("净资产收益率", "roe", "毛利率"),
        "title": "盈利能力对比",
        "label": "ROE",
        "exclude": ("分位", "百分位"),
    },
    {
        "keys": ("归母净利润", "净利润", "营业收入", "营收"),
        "title": "财务对比",
        "label": "净利润同比",
        "exclude": (),
    },
]


def _iwc_col_key(col: dict) -> str:
    """列在 datas 行里的取值键（key > feKey > index_name）。"""
    for k in ("key", "feKey", "index_name"):
        v = col.get(k)
        if v:
            return str(v)
    return ""


def _iwc_base_name(col: dict) -> str:
    """去掉日期后缀：'收盘价[20261102]' → '收盘价'。"""
    name = _iwc_col_key(col)
    base = _IWC_TS_SUFFIX_RE.sub("", name).strip()
    return base or name


def _iwc_col_ts(col: dict) -> str:
    """列日期：优先 timestamp 字段，否则从列名后缀解析（后端只保证一种）。"""
    ts = str(col.get("timestamp") or "").strip()
    if ts:
        return ts
    m = _IWC_TS_SUFFIX_RE.search(_iwc_col_key(col))
    return m.group(1).strip() if m else ""


def _iwc_is_range_ts(ts: str) -> bool:
    return bool(_IWC_RANGE_TS_RE.search(str(ts or "")))


def _iwc_is_numeric(col: dict, rows: list) -> bool:
    if str(col.get("type") or "").upper() in _IWC_NUMERIC_TYPES:
        return True
    key = _iwc_col_key(col)
    for row in rows[:5]:
        if isinstance(row, dict) and _to_float(row.get(key)) is not None:
            return True
    return False


def _iwc_entity_cols(cols: list) -> tuple:
    """找出 (名称列, 代码列) 的取值键；找不到返回空串。"""
    name_key = code_key = ""
    for col in cols:
        key = _iwc_col_key(col)
        label = str(col.get("label") or "")
        index_name = str(col.get("index_name") or "")
        if not code_key and (label in _IWC_CODE_LABELS or index_name in _IWC_CODE_KEYS):
            code_key = key
        if not name_key and (label in _IWC_NAME_LABELS or index_name in _IWC_NAME_KEYS):
            name_key = key
    return name_key, code_key


def _iwc_make_fragment(chart_type: str, code: str, name: str, title: str,
                       option: dict, dialog_uuid: str = "") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "dialog_uuid": dialog_uuid,
        "chart_type": chart_type,
        "stock_code": code,
        "stock_name": name,
        "title": title,
        "data_json": json.dumps(option, ensure_ascii=False),
        "annotations": "[]",
        "source": "rule",
    }


def _iwc_series_fragments(cols: list, rows: list, name_key: str, code_key: str,
                          dialog_uuid: str = "") -> list:
    """宽表时间序列 → 折线图（同一指标带多个日期后缀列）。"""
    groups: dict = {}
    for col in cols:
        ts = _iwc_col_ts(col)
        if not ts or _iwc_is_range_ts(ts):
            continue
        if not _iwc_is_numeric(col, rows):
            continue
        groups.setdefault(_iwc_base_name(col), []).append(col)

    out = []
    for base, group in groups.items():
        by_day: dict = {}
        for col in group:
            m = _IWC_DAY_RE.search(_iwc_col_ts(col))
            if not m:
                continue
            by_day.setdefault(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", col)
        if len(by_day) < 5:          # 少于 5 个点画折线没意义
            continue
        days = sorted(by_day)[-_SERIES_MAX_POINTS:]
        series = []
        for row in rows[:_SERIES_MAX_ROWS]:
            if not isinstance(row, dict):
                continue
            entity = ""
            if name_key or code_key:
                entity = str(row.get(name_key) or row.get(code_key) or "")
            values = [_to_float(row.get(_iwc_col_key(by_day[d]))) for d in days]
            if all(v is None for v in values):
                continue
            series.append({"name": f"{entity} {base}".strip(), "type": "line", "data": values})
        if not series:
            continue
        code = ""
        if rows and isinstance(rows[0], dict) and code_key:
            code = str(rows[0].get(code_key) or "")
        option = {"xAxis": [{"data": days}], "series": series}
        out.append(_iwc_make_fragment(
            "time_series", code, "", f"{base}走势", option, dialog_uuid))
        if len(out) >= 2:            # 一次输出最多 2 张折线
            break
    return out


def _iwc_pick_metric(latest: dict, pat: dict):
    """按优先级为该规则挑一列；找不到返回 None。

    latest: {base_name: (ts, col)}，同一业务概念的多列都在里面。
    """
    exclude = tuple(str(x).lower() for x in (pat.get("exclude") or ()))
    for key in pat["keys"]:
        k = str(key).lower()
        # 同 key 命中多列时，列名越短越「纯」（避免挑到 xxx:前复权同比增长率）
        candidates = sorted(
            (base for base in latest if k in base.lower()),
            key=lambda b: (len(b), b),
        )
        for base in candidates:
            if any(x in base.lower() for x in exclude):
                continue
            return latest[base][1], base
    return None


def _iwc_cross_fragments(cols: list, rows: list, name_key: str, code_key: str,
                         dialog_uuid: str = "") -> list:
    """截面（多标的 × 多指标）→ 对比柱状图（一图一指标）。"""
    if len(rows) < 2:
        return []
    entity_key = name_key or code_key
    if not entity_key:
        return []

    # 每个指标只留「最新一期」那一列；区间合计列（涨跌幅[2026xxxx-2026xxxx]）丢弃
    latest: dict = {}
    for col in cols:
        ts = _iwc_col_ts(col)
        if ts and _iwc_is_range_ts(ts):
            continue
        if not _iwc_is_numeric(col, rows):
            continue
        key = _iwc_col_key(col)
        if key in (name_key, code_key):
            continue
        base = _iwc_base_name(col)
        prev = latest.get(base)
        if prev is None or ts >= prev[0]:
            latest[base] = (ts, col)
    if not latest:
        return []

    sample = rows[:_CROSS_MAX_ROWS]
    labels = [str(r.get(entity_key) or "") if isinstance(r, dict) else "" for r in sample]
    if not any(labels):
        return []

    out = []
    for pat in _CROSS_PATTERNS:
        if len(out) >= _CROSS_MAX_CHARTS:
            break
        picked = _iwc_pick_metric(latest, pat)
        if not picked:
            continue
        col, base = picked
        key = _iwc_col_key(col)
        data = [_to_float(r.get(key)) if isinstance(r, dict) else None for r in sample]
        if all(v is None for v in data):
            continue
        # 标题带上首个标的与样本数：既让人一眼知道范围，也避免同一份报告里
        # 「涨跌幅对比」标题重名（report_builder 按 title 去重，重名只会剩最后一张）
        first = next((x for x in labels if x), "")
        n = sum(1 for x in labels if x)
        scope = f"{first}等{n}只" if n > 1 else first
        title = f"{pat['title']}·{scope}" if scope else pat["title"]
        option = {
            "xAxis": [{"data": labels}],
            "series": [{"name": _short_metric_name(base),
                        "type": "bar", "data": data}],
        }
        out.append(_iwc_make_fragment(
            pat.get("chart_type", "comparison"), "", "", title, option, dialog_uuid))
    return out


def extract_from_iwc(data: dict, dialog_uuid: str = "") -> list:
    """iwc_query 输出 → ChartFragment 列表（未入库）。

    先试宽表时间序列（折线），没有再退回截面（对比柱状）。两者都拿不到就返回 []。
    任何异常都返回 []（调用方还有一层 try/except）。
    """
    try:
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return []
        cols = payload.get("columns") or []
        rows = payload.get("datas") or []
        if not cols or not rows:
            return []
        cols = [c for c in cols if isinstance(c, dict)]
        name_key, code_key = _iwc_entity_cols(cols)
        frags = _iwc_series_fragments(cols, rows, name_key, code_key, dialog_uuid)
        if frags:
            return frags
        return _iwc_cross_fragments(cols, rows, name_key, code_key, dialog_uuid)
    except Exception:
        return []


# ── K 线类工具（get_etf_history 等）→ 收盘价 + 均线折线图 ──
#
# 为什么不做成蜡烛图：普通 @@CHART 片段只支持 line/bar（前端 _buildFragmentOption
# 只认 series[].type），真蜡烛图必须走 tech_panel，而 tech_panel 的数据来自本地
# market_data.db。ETF 的本地覆盖极差（1710 只里只有 71 只有 ≥60 根日 K），
# 所以这类标的用工具当场返回的 OHLCV 画折线兜底。

_BAR_LINE_MAX_POINTS = 120


def _moving_average(values: list, n: int) -> list:
    """简单移动平均，不足 n 个点的位置补 None（保证与 x 轴等长）。"""
    out = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
            continue
        window = [v for v in values[i - n + 1:i + 1] if v is not None]
        out.append(round(sum(window) / n, 4) if len(window) == n else None)
    return out


def extract_from_bars(bars: list, code: str = "", name: str = "",
                      dialog_uuid: str = "") -> list:
    """OHLCV 序列 → 一张「收盘价 + MA5 + MA20」折线图。

    bars 元素形如 {"date": "2026-11-02", "open":…, "close":…, "volume":…}；
    取不到至少 5 根就返回 []（点太少画折线没意义）。
    """
    try:
        if not isinstance(bars, list):
            return []
        rows = [b for b in bars
                if isinstance(b, dict) and b.get("date") and b.get("close") is not None]
        if len(rows) < 5:
            return []
        rows = rows[-_BAR_LINE_MAX_POINTS:]
        dates = [str(b["date"]) for b in rows]
        closes = [_to_float(b.get("close")) for b in rows]
        series = [{"name": "收盘价", "type": "line", "data": closes}]
        for n in (5, 20):
            if len(closes) >= n:
                series.append({"name": f"MA{n}", "type": "line",
                               "data": _moving_average(closes, n)})
        option = {"xAxis": [{"data": dates}], "series": series}
        title = f"{name or code} 收盘价与均线".strip()
        return [_iwc_make_fragment("time_series", str(code or ""), str(name or ""),
                                   title, option, dialog_uuid)]
    except Exception:
        return []


# ── chart_fragments 持久化（stock_radar.db，DDL 单一事实源 utils/db_schema） ──


def get_chart_db_path() -> str:
    """stock_radar.db 路径（与 Config.get_db_path 同源）。"""
    try:
        from utils.app_paths import get_db_path
        return get_db_path()
    except Exception:
        return os.path.join(os.getcwd(), "stock_radar.db")


@contextmanager
def _chart_db():
    conn = sqlite3.connect(get_chart_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        from utils.db_schema.migrator import ensure_schema
        from utils.db_schema.tables.stock_radar import TABLES
        ensure_schema(conn, [t for t in TABLES if t.name == "chart_fragments"])
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_fragments(fragments: list[dict]) -> int:
    """批量入库，按 (stock_code, title, data_json) 幂等去重，返回实际插入数。"""
    inserted = 0
    with _chart_db() as conn:
        for f in fragments:
            exists = conn.execute(
                "SELECT 1 FROM chart_fragments WHERE stock_code=? AND title=? AND data_json=?",
                (f["stock_code"], f["title"], f["data_json"]),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO chart_fragments
                   (id, dialog_uuid, chart_type, stock_code, stock_name, title, data_json, annotations, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (f["id"], f["dialog_uuid"], f["chart_type"], f["stock_code"],
                 f["stock_name"], f["title"], f["data_json"], f["annotations"], f["source"]),
            )
            inserted += 1
    return inserted


_STOCK_CODE_RE = re.compile(r"\d{6}")


# ═══════════════════════════════════════════════════════════════
# 技术面图板（tech_panel）：基于本地 K 线库的报告增强图卡
#
# 报告归集 @@CHART@@ 标记时，对涉及的主股票尝试生成一张交互式
# 技术面图板（前端 Tab 切换 MACD/RSI/KDJ/BOLL/DPO + 金叉死叉/
# 突破信号标注）。数据来自本地 market_data.db（近 250 个交易日），
# 指标与信号由 /api/chart/kline 与 /api/chart/signals 在前端渲染时
# 拉取，本函数只负责"探测 + 生成轻量 fragment 入库"。
# 标的类型（个股 / 指数）由 data_json.market 显式携带，绝不由裸码猜表名。
# 有枣打两棍子：本地无 K 线数据或任何异常 → 返回 None，不影响报告。
# ═══════════════════════════════════════════════════════════════

_TECH_PANEL_MIN_BARS = 60   # 本地少于 60 根日 K 不生成（画出来没意义）

# 指数代码形态：纯数字（000001/399001/930914）或「字母+数字」（H11001/H30269）
# 或纯字母（HSI/HSTECH）。个股仍沿用严格 6 位数字校验。
_INDEX_CODE_RE = re.compile(r"[A-Za-z0-9]{2,10}")


def has_local_kline(code: str, min_bars: int = _TECH_PANEL_MIN_BARS,
                    market: str = None) -> bool:
    """探测本地日线是否够画图板。

    market='INDEX' 时查 index_daily（指数），否则查 stock_daily（个股/ETF/板块）。
    必须显式区分：`000001` 在两张表里是两个不同标的（上证指数 / 平安银行）。
    """
    code = str(code or "").strip()
    if not code:
        return False
    if market == "INDEX":
        if not _INDEX_CODE_RE.fullmatch(code):
            return False
        try:
            from utils.cache.market_data_db import get_index_daily_count
            return get_index_daily_count(code) >= min_bars
        except Exception:
            return False
    if not re.fullmatch(r"\d{5,6}", code):
        return False
    try:
        from utils.cache.market_data_db import get_stock_daily_count
        return get_stock_daily_count(code) >= min_bars
    except Exception:
        return False


def ensure_tech_panel(code: str, name: str = "", dialog_uuid: str = "",
                      market: str = None) -> Optional[str]:
    """为股票/指数生成（或复用已有的）技术面图板 fragment，返回 fragment id。

    - market: 'INDEX' 走 index_daily、名称取 index_info；'STOCK' 走 stock_daily、
      名称取 stock_info；None 时按 `resolve_symbol_market(code, name)` 自动判定
      （name_hint 命中指数名即可识别指数，如 fragment 的 stock_name='上证指数'）。
    - data_json 会带上 market，前端据此把 `market=INDEX` 传给 /api/chart/kline
      与 /api/chart/signals —— 否则同码会被解析成个股。
    - 幂等：insert_fragments 按 (stock_code, title, data_json) 去重，同参数
      重复调用返回既有记录。本地 K 线数据不足或异常时返回 None。
    """
    code = str(code or "").strip()
    market = str(market or "").strip().upper()
    if market not in ("INDEX", "STOCK"):
        try:
            from utils.cache.market_data_db import resolve_symbol_market
            market = resolve_symbol_market(code, name)
        except Exception:
            market = "STOCK"
    if not has_local_kline(code, market=market):
        return None
    try:
        if not name:
            try:
                if market == "INDEX":
                    from utils.cache.market_data_db import get_index_name
                    name = str(get_index_name(code) or "")
                else:
                    from utils.cache.market_data_db import get_stock_info
                    info = get_stock_info(code)
                    if info and info.get("name"):
                        name = str(info["name"])
            except Exception:
                name = ""
        fragment = {
            "id": str(uuid.uuid4()),
            "dialog_uuid": dialog_uuid,
            "chart_type": "tech_panel",
            "stock_code": code,
            "stock_name": name,
            "title": f"{name or code} 技术面图板",
            # 前端按 data_json.market 决定拉取通道：
            # /api/chart/kline/{code}?days=250&market=INDEX，指数走 index_daily
            "data_json": json.dumps({"code": code, "name": name, "days": 250,
                                     "market": market},
                                    ensure_ascii=False),
            "annotations": "[]",
            "source": "enhanced",
        }
        inserted = insert_fragments([fragment])
        if inserted:
            return fragment["id"]
        # 已存在（幂等命中）→ 查回既有 id
        with _chart_db() as conn:
            row = conn.execute(
                "SELECT id FROM chart_fragments WHERE stock_code=? AND title=? AND data_json=?",
                (fragment["stock_code"], fragment["title"], fragment["data_json"]),
            ).fetchone()
            return row["id"] if row else None
    except Exception:
        return None


def extract_from_tool_output(tool_name: str, content: str, dialog_uuid: str = "",
                             tool_args: dict = None) -> list[str]:
    """Agent 集成入口：从工具输出 JSON 萃取图表并入库，返回图表 ID 列表。

    支持的工具在 ``CHART_TOOLS`` 里（mx_data_query / iwc_query / get_etf_history）；
    ``tool_args`` 只在 K 线类工具里用来取标的代码（输出本身不带 code）。

    设计约束（效率/失败隔离）：
    - 同步执行但为纯 CPU + 本地 SQLite，毫秒级，不调 LLM
    - 任何异常都返回 []，绝不上抛（调用方再包 try/except 双保险）
    - stock_code 从 query 中正则提取（6 位数字），提取不到留空
    """
    if tool_name not in CHART_TOOLS:
        return []
    try:
        data = json.loads(content)
    except Exception:
        return []
    try:
        fragments: list = []
        if tool_name == "iwc_query":
            fragments = extract_from_iwc(data, dialog_uuid=dialog_uuid)
        elif tool_name in _BAR_TOOLS:
            args = tool_args if isinstance(tool_args, dict) else {}
            code = str(args.get("symbol") or args.get("code") or "")
            if isinstance(data, list):
                bars = data
            elif isinstance(data, dict):
                bars = data.get("latest_5") or data.get("data") or []
            else:
                bars = []
            fragments = extract_from_bars(bars, code=code, dialog_uuid=dialog_uuid)
        else:
            tables = data.get("tables") if isinstance(data, dict) else None
            if not tables:
                return []
            query = str(data.get("query", ""))
            m = _STOCK_CODE_RE.search(query)
            stock_code = m.group(0) if m else ""
            fragments = extract_from_tables(tables, stock_code=stock_code,
                                            stock_name="", dialog_uuid=dialog_uuid)
        if not fragments:
            return []
        insert_fragments(fragments)
        return [f["id"] for f in fragments]
    except Exception:
        return []


def list_fragments(limit: int = 50) -> list[dict]:
    with _chart_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chart_fragments ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_fragment(fragment_id: str) -> Optional[dict]:
    with _chart_db() as conn:
        row = conn.execute(
            "SELECT * FROM chart_fragments WHERE id=?", (fragment_id,)
        ).fetchone()
    return dict(row) if row else None


def clear_fragments() -> int:
    with _chart_db() as conn:
        cur = conn.execute("DELETE FROM chart_fragments")
        return cur.rowcount
