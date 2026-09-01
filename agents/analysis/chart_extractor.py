"""图表片段萃取器（Track 1 规则萃取）— 对应 docs/superpowers/specs/2026-07-06-chart-fragments-design.md

从 mx_data 工具返回的结构化 tables（fieldnames + rows）自动生成 ECharts-ready 图表数据。
与 agent 流程解耦：chart_debug CLI 与未来 agent 集成共用本模块，验证通过后集成零重写。

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
    """宽松转数值：兼容 %、亿/万等单位后缀和千分位。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    return float(m.group(0))


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


def extract_from_tables(tables: list, stock_code: str = "", stock_name: str = "",
                        dialog_uuid: str = "") -> list[dict]:
    """规则萃取主入口：扫描 tables → 生成 ChartFragment dict 列表（未入库）。

    tables: mx_data 返回的 [{"sheet_name", "fieldnames", "rows"}, ...]
    实体标注：优先取每个表格 sheet_name 里的真实实体（多股查询各表独立），
    提取不到时回退到调用方传入的 stock_code/stock_name。
    """
    fragments = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        fieldnames = table.get("fieldnames") or []
        rows = table.get("rows") or []
        if not fieldnames or not rows:
            continue
        # 表级实体（真实数据源），回退到调用方参数
        entity_name, entity_code = _extract_entity(table)
        code = entity_code or stock_code
        name = entity_name or stock_name
        for pat, matched_cols in _match_patterns(fieldnames):
            data_json = _build_data_json(pat, fieldnames, rows, matched_cols)
            if data_json is None:
                continue
            fragments.append({
                "id": str(uuid.uuid4()),
                "dialog_uuid": dialog_uuid,
                "chart_type": pat["chart_type"],
                "stock_code": code,
                "stock_name": name,
                "title": f"{name or code} {pat['title']}",
                "data_json": json.dumps(data_json, ensure_ascii=False),
                "annotations": "[]",
                "source": "rule",
            })
    return fragments


# ── SQLite 持久化（stock_radar.db 的 chart_fragments 表，设计文档定义） ──

_DDL = """
CREATE TABLE IF NOT EXISTS chart_fragments (
    id            TEXT PRIMARY KEY,
    dialog_uuid   TEXT NOT NULL,
    chart_type    TEXT NOT NULL,
    stock_code    TEXT DEFAULT '',
    stock_name    TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    data_json     TEXT NOT NULL,
    annotations   TEXT DEFAULT '[]',
    source        TEXT DEFAULT 'rule',
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chart_fragments_dialog ON chart_fragments(dialog_uuid);
"""


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
        conn.executescript(_DDL)  # 多语句 DDL 需 executescript
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


def extract_from_tool_output(tool_name: str, content: str, dialog_uuid: str = "") -> list[str]:
    """Agent 集成入口：从工具输出 JSON 萃取图表并入库，返回图表 ID 列表。

    设计约束（效率/失败隔离）：
    - 同步执行但为纯 CPU + 本地 SQLite，毫秒级，不调 LLM
    - 任何异常都返回 []，绝不上抛（调用方再包 try/except 双保险）
    - stock_code 从 query 中正则提取（6 位数字），提取不到留空
    """
    if tool_name != "mx_data_query":
        return []
    try:
        data = json.loads(content)
        tables = data.get("tables") or []
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
