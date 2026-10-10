"""工具输出规则压缩（Step 2 P0 + Step 4/5 P1/P2 规则集）。

设计见 docs/prd/工具输出压缩规则化-调研分析.md：
- 规则压缩只做确定性变换；无规则 / 解析失败 / 任何异常时原样返回，绝不阻塞工具流程；
- 模式由 Config 控制：off / shadow（双算并落盘，不改变现有输出）/ on（采用规则输出）；
- LLM 压缩仍作为兜底（agents/react/utils.py::_compress_tool_output）；
- 查询自适应：压缩器接收 tool_args，按查询决定保留哪些字段（如查询含「概念/板块/行业」时保留对应列）。

已覆盖工具：
- mx_xuangu_filter     选股结果 → 查询自适应列表格
- mx_search_news       资讯搜索 → 修断行数字 + 按条预算摘要
- iwc_query            问财统一查询（选股/行情/财务/指数/行业/…） → 去 columns 元数据 + 表格
- get_positions        持仓 → 紧凑表格 + 账户摘要
- get_hot_stocks / get_limit_up_pool / get_etf_history  榜单类 → 通用列表格
- get_lianban_ladder   连板天梯 → 按板数分组
- iwc_search           问财统一检索（news/announcement/report） → 标题 + 摘要预算
"""
import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

SHADOW_LOG_FILENAME = "tool_compress_shadow.jsonl"
SHADOW_DETAIL_PREFIX = "tool_compress_shadow_detail"
SHADOW_DETAIL_CONTENT_LIMIT = 4000

_NUMBER_BREAK_RE = re.compile(r"(\d)\.\s*\n(\d)")
_DATE_SUFFIX_RE = re.compile(r"\s+\d{4}[.\-]\d{2}[.\-]?\d{0,2}$")
_NEWS_ITEM_RE = re.compile(r"\n(?=--- \d+\. )")
_NEWS_HEAD_RE = re.compile(r"--- (\d+)\. (.+) ---")
_NEWS_META_RE = re.compile(r"日期:\s*([\d-]+)\s*\|\s*类型:\s*(\S+)")
_NEWS_META_STRIP_RE = re.compile(r"日期:.*?类型:\s*\S+")

NEWS_BODY_LIMIT = 280
CELL_LIMIT = 300
ANNOUNCEMENT_SUMMARY_LIMIT = 200
MX_DATA_MAX_COLUMNS = 16
MX_DATA_MAX_TABLES = 5

_XG_CORE_KEYS = re.compile(
    r"代码|名称|简称|最新价|涨跌幅|涨跌额|成交额|成交量|换手率|量比|振幅|市盈率|市净率|市值|开盘价|最高价|最低价"
)
_XG_EXTRA_KEYS = re.compile(r"概念|板块|行业|题材")
_XG_DROP_KEYS = {
    "序号",
    "主营产品",
    "CHOICE_INNER_CODE",
    "IN_OPTIONAL",
    "成份股",
    "中证行业指数成份股",
    "证券类型",
    "东财行业龙头",
}

# 有损标记：输出中出现这些标记表示已确定性丢数据，on 模式不得据此跳过 LLM
LOSSY_MARKERS = ("…(另", "个表已省略")

_shadow_lock = threading.Lock()


def preprocess(content: str) -> str:
    """确定性预处理：修复被换行拆开的数字（'6013.\\n08' → '6013.08'）。"""
    if not content:
        return content
    return _NUMBER_BREAK_RE.sub(r"\1.\2", content)


# ── 通用渲染 ──

def _norm_key(key) -> str:
    return _DATE_SUFFIX_RE.sub("", str(key)).strip()


def _cell_text(value, limit: int = CELL_LIMIT) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _render_markdown_table(rows: list, keys: list, title: str, cell_limit: int = CELL_LIMIT) -> str:
    lines = [
        title,
        "| " + " | ".join(_norm_key(key) for key in keys) + " |",
        "|" + "---|" * len(keys),
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append("| " + " | ".join(_cell_text(row.get(key, ""), cell_limit) for key in keys) + " |")
    return "\n".join(lines)


def _is_degenerate(rows: list, key: str) -> bool:
    """列是否无信息量：全空 / 全 null / 全数值 0（bool 除外）。"""
    values = [row.get(key) for row in rows if isinstance(row, dict)]
    meaningful = [v for v in values if v not in (None, "", "null", "None")]
    if not meaningful:
        return True
    numeric = []
    for value in meaningful:
        if isinstance(value, bool):
            return False
        try:
            numeric.append(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return False
    return all(item == 0 for item in numeric)


def _pick_row_keys(rows: list, drop: set | None = None) -> list:
    drop = drop or set()
    keys = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            if key not in drop and key not in keys:
                keys.append(key)
    return [key for key in keys if not _is_degenerate(rows, key)]


# ── mx_xuangu_filter（P0） ──

def _query_wants_column(query: str, column: str) -> bool:
    """判断查询是否点名某个列（列名去括号后与原查询做包含 / 中文 2-gram 匹配）。"""
    if not query or not column:
        return False
    base = re.sub(r"[（(].*?[)）]", "", _norm_key(column))
    base = re.sub(r"[^\w\u4e00-\u9fff]", "", base).lower()
    if not base:
        return False
    q = query.lower()
    if base in q:
        return True
    grams = {
        base[index:index + 2]
        for index in range(len(base) - 1)
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", base[index:index + 2])
    }
    return any(gram in q for gram in grams)


def _select_xuangu_keys(stocks: list, query: str) -> list:
    extra_active = bool(_XG_EXTRA_KEYS.search(query or ""))
    keys = []
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        for key in stock:
            normalized = _norm_key(key)
            if normalized in _XG_DROP_KEYS:
                continue
            if (
                _XG_CORE_KEYS.search(normalized)
                or (extra_active and _XG_EXTRA_KEYS.search(normalized))
                or _query_wants_column(query, key)
            ):
                if key not in keys:
                    keys.append(key)
    return keys


def _compress_mx_xuangu_filter(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    stocks = data.get("stocks")
    if not isinstance(stocks, list) or not stocks:
        return content
    query = str((tool_args or {}).get("query") or "")
    keys = _select_xuangu_keys(stocks, query)
    if not keys:
        return content
    return _render_markdown_table(stocks, keys, f"# 选股结果 {len(stocks)} 只")


# ── mx_search_news（P1） ──

def _compress_mx_search_news(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    result = data.get("result")
    if not result or not isinstance(result, str):
        return content
    text = preprocess(result)
    out = []
    for item in _NEWS_ITEM_RE.split(text):
        match = _NEWS_HEAD_RE.match(item)
        if not match:
            continue
        index, title = match.group(1), match.group(2).strip()
        meta = ""
        meta_match = _NEWS_META_RE.search(item)
        if meta_match:
            meta = f"{meta_match.group(1)} {meta_match.group(2)}"
        body = _NEWS_META_STRIP_RE.sub("", item[match.end():], count=1).strip()
        body = preprocess(body)
        if len(body) > NEWS_BODY_LIMIT:
            body = body[:NEWS_BODY_LIMIT].rstrip() + "…"
        out.append(f"[{index}] {title} ({meta})\n{body}")
    if not out:
        return content
    return json.dumps(
        {"query": data.get("query", ""), "result": "\n\n".join(out)},
        ensure_ascii=False,
    )


# ── iwc 问财类（P2） ──

def _compress_iwc_query(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    inner = data.get("data")
    if not isinstance(inner, dict):
        return content
    rows = inner.get("datas")
    if not isinstance(rows, list) or not rows:
        return content

    status_code = inner.get("status_code")
    status_msg = inner.get("status_msg")
    if status_code not in (None, 0) and status_msg:
        return content

    column_keys = [
        column.get("key")
        for column in (inner.get("columns") or [])
        if isinstance(column, dict) and column.get("key")
    ]
    keys = [key for key in column_keys if any(key in row for row in rows if isinstance(row, dict))]
    extras = [key for key in _pick_row_keys(rows) if key not in keys]
    keys = keys + extras
    keys = [key for key in keys if not _is_degenerate(rows, key)]
    if not keys:
        return content
    return _render_markdown_table(rows, keys, f"# 查询结果 {len(rows)} 行")


def _compress_iwc_search(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    inner = data.get("data")
    items = inner.get("data") if isinstance(inner, dict) else None
    if not isinstance(items, list) or not items:
        return content
    lines = [f"# 公告/资讯 {len(items)} 条"]
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = preprocess(str(item.get("summary") or "")).strip()
        if not title and not summary:
            continue
        lines.append(f"[{index}] {title}")
        if summary:
            lines.append(summary[:ANNOUNCEMENT_SUMMARY_LIMIT].rstrip() + ("…" if len(summary) > ANNOUNCEMENT_SUMMARY_LIMIT else ""))
    if len(lines) == 1:
        return content
    return "\n".join(lines)


# ── 持仓类（P2） ──

_POSITION_KEYS = ["stock_code", "stock_name", "qty", "avg_cost", "avail_qty", "realized_pnl", "is_etf"]


def _compress_get_positions(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    positions = data.get("positions")
    if not isinstance(positions, list) or not positions:
        return content
    keys = [key for key in _POSITION_KEYS if any(key in row for row in positions if isinstance(row, dict))]
    if not keys:
        keys = _pick_row_keys(positions, drop={"updated_at", "source", "realized_pnl"})
    if not keys:
        return content
    lines = [_render_markdown_table(positions, keys, f"# 持仓 {len(positions)} 只")]
    account = data.get("account")
    if isinstance(account, dict):
        parts = [f"{_norm_key(key)}={value}" for key, value in account.items() if value is not None]
        if parts:
            lines.append("账户: " + " ｜ ".join(parts))
    meta = data.get("meta")
    note = meta.get("note") if isinstance(meta, dict) else None
    if note:
        lines.append("备注: " + str(note)[:200])
    return "\n".join(lines)


# ── 榜单类（P2，通用列表格） ──

_DATE_VALUE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")


def _looks_like_date(value) -> bool:
    return bool(_DATE_VALUE_RE.match(str(value or "").strip()))


def _compress_plain_rows(content: str, tool_args: dict, title: str) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, list) or not data:
        return content
    if not any(isinstance(row, dict) for row in data):
        return content
    keys = _pick_row_keys(data)
    if not keys:
        return content
    # 时间序列倒序渲染（最新在前）：后续硬截断/压缩只会砍掉最旧的行，
    # 避免 2026-09-17 实测的「8000 字符头截断砍掉最近 14 个交易日」问题。
    if _looks_like_date(data[0].get("date")):
        ordered = sorted(data, key=lambda row: str(row.get("date") or ""), reverse=True)
        title = f"{title}（按时间倒序，最新在前）"
    else:
        ordered = data
    return _render_markdown_table(ordered, keys, f"# {title} {len(ordered)} 条")


def _compress_get_lianban_ladder(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, list) or not data:
        return content
    lines = []
    total = 0
    for day in data:
        if not isinstance(day, dict):
            continue
        boards = day.get("boards")
        if not isinstance(boards, list) or not boards:
            continue
        lines.append(f"## {day.get('date', '')}")
        grouped = defaultdict(list)
        for board in boards:
            if isinstance(board, dict):
                grouped[board.get("board_num")].append(board)
        for board_num in sorted(grouped, key=lambda n: (n is None, -(n or 0))):
            items = grouped[board_num]
            names = ", ".join(f"{b.get('name')}({b.get('code')})" for b in items)
            lines.append(f"{board_num}板({len(items)}): {names}")
            total += len(items)
    if not lines:
        return content
    return "\n".join([f"# 连板天梯 {total} 只"] + lines)


# ── mx_data_query（P2 残余：转置宽表 + 多表封顶） ──

def _truncate_table_columns(lines: list, max_columns: int) -> list:
    """裁剪 markdown 表格列数：保留前 max_columns 列 + 截断标记列。"""
    if not lines:
        return lines
    cell_count = len(lines[0].split("|")) - 2
    if cell_count <= max_columns:
        return lines

    skipped = cell_count - max_columns
    out = []
    for index, line in enumerate(lines):
        cells = line.split("|")[1:-1]
        kept = cells[:max_columns]
        if index == 0:
            kept.append(f"…(另{skipped}列)")
        elif index == 1:
            kept.append("---")
        else:
            kept.append("…")
        out.append("|" + "|".join(kept) + "|")
    return out


def _compress_terminal_output(terminal: str, max_columns: int, max_tables: int) -> tuple[str, bool]:
    """处理 terminal_output：宽表截列、多表封顶。返回 (新文本, 是否变更)。"""
    lines = terminal.split("\n")
    tables = []
    current = []
    for index, line in enumerate(lines):
        if line.strip().startswith("|"):
            current.append(index)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    if not tables:
        return terminal, False

    changed = False
    dropped = set()
    for table_index, positions in enumerate(tables):
        if table_index >= max_tables:
            dropped.update(positions)
            changed = True
            continue
        table_lines = [lines[position] for position in positions]
        truncated = _truncate_table_columns(table_lines, max_columns)
        if truncated != table_lines:
            changed = True
            for offset, position in enumerate(positions):
                lines[position] = truncated[offset]

    if not changed:
        return terminal, False

    marker = None
    insert_after = None
    if len(tables) > max_tables:
        titles = []
        for positions in tables[max_tables:]:
            for position in range(positions[0] - 1, -1, -1):
                candidate = lines[position].strip()
                if candidate.startswith("**") and candidate.endswith("**"):
                    titles.append(candidate.strip("*").strip())
                    break
        marker = f"（其余 {len(tables) - max_tables} 个表已省略"
        if titles:
            marker += "：" + "；".join(titles[:8]) + (" 等" if len(titles) > 8 else "")
        marker += "）"
        insert_after = max(position for positions in tables[:max_tables] for position in positions)

    out = []
    for index, line in enumerate(lines):
        if index in dropped:
            continue
        out.append(line)
        if insert_after is not None and index == insert_after:
            out.append(marker)
    return "\n".join(out), True


def _compress_mx_data_query(content: str, tool_args: dict) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(data, dict):
        return content
    terminal = data.get("terminal_output")
    if not terminal or not isinstance(terminal, str):
        return content
    processed, changed = _compress_terminal_output(terminal, MX_DATA_MAX_COLUMNS, MX_DATA_MAX_TABLES)
    if not changed:
        return content
    result = dict(data)
    result["terminal_output"] = processed
    result.pop("tables", None)
    return json.dumps(result, ensure_ascii=False)


# ── 注册表 ──

RULE_COMPRESSORS = {
    "mx_xuangu_filter": _compress_mx_xuangu_filter,
    "mx_search_news": _compress_mx_search_news,
    "mx_data_query": _compress_mx_data_query,
    "iwc_query": _compress_iwc_query,
    "iwc_search": _compress_iwc_search,
    "get_positions": _compress_get_positions,
    "get_hot_stocks": lambda content, args: _compress_plain_rows(content, args, "热股榜"),
    "get_limit_up_pool": lambda content, args: _compress_plain_rows(content, args, "涨停池"),
    "get_limit_break_pool": lambda content, args: _compress_plain_rows(content, args, "炸板池"),
    "get_etf_history": lambda content, args: _compress_plain_rows(content, args, "ETF 历史行情"),
    "get_lianban_ladder": _compress_get_lianban_ladder,
}


def compress(tool_name: str, tool_args: dict, content: str) -> str:
    """规则压缩入口（策略无关，供回放/测试直接调用）。异常时原样返回。"""
    if not content:
        return content
    rule = RULE_COMPRESSORS.get(tool_name)
    if rule is None:
        return content
    try:
        processed = preprocess(content)
        result = rule(processed, tool_args or {})
        return result if isinstance(result, str) else content
    except Exception:
        return content


def is_lossy_output(tool_name: str, content: str) -> bool:
    """规则输出是否含确定性丢数据标记（on 模式不得据此跳过 LLM）。"""
    if not content:
        return False
    return any(marker in content for marker in LOSSY_MARKERS)


def get_mode(tool_name: str) -> str:
    """读取当前工具规则压缩模式（off/shadow/on），默认 off。"""
    try:
        from config import Config

        if not getattr(Config, "TOOL_RULE_COMPRESS_ENABLED", False):
            return MODE_OFF
        raw_policy = getattr(Config, "TOOL_RULE_COMPRESS_POLICY", "") or ""
        if raw_policy:
            policy = json.loads(raw_policy)
            if isinstance(policy, dict):
                mode = str(policy.get(tool_name, policy.get("default", MODE_OFF))).lower()
                if mode in _VALID_MODES:
                    return mode
    except Exception:
        pass
    return MODE_OFF


def log_shadow_event(
    tool_name: str,
    tool_args: dict,
    mode: str,
    raw_len: int,
    rule_len: int,
    final_len: int,
    llm_used: bool,
    threshold: int,
    raw_content: str = "",
    rule_content: str = "",
    final_content: str = "",
    rule_lossy: bool = False,
) -> None:
    """影子/启用模式的对比数据落盘（失败静默）。

    - `logs/tool_compress_shadow.jsonl`：每次一行统计（长度、是否跳过、query）；
    - `logs/tool_compress_shadow_detail-YYYYMMDD.jsonl`：含原始/规则/最终内容（各截 4000 字），
      供 scripts/devtools/tool_compress_shadow_report.py 生成逐条对照表。
    """
    try:
        from utils.app_paths import get_logs_dir

        now = datetime.now(timezone.utc)
        record = {
            "ts": now.isoformat(),
            "tool": tool_name,
            "mode": mode,
            "query": str((tool_args or {}).get("query") or "")[:200],
            "raw_len": raw_len,
            "rule_len": rule_len,
            "final_len": final_len,
            "llm_used": bool(llm_used),
            "rule_would_skip_llm": bool(threshold > 0 and 0 < rule_len <= threshold),
            "rule_lossy": bool(rule_lossy),
        }
        directory = get_logs_dir()
        os.makedirs(directory, exist_ok=True)
        with _shadow_lock:
            with open(os.path.join(directory, SHADOW_LOG_FILENAME), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

            def clip(text: str) -> str:
                text = text or ""
                if len(text) <= SHADOW_DETAIL_CONTENT_LIMIT:
                    return text
                return text[:SHADOW_DETAIL_CONTENT_LIMIT]

            detail = dict(record)
            detail["date"] = now.strftime("%Y%m%d")
            detail["threshold"] = threshold
            detail["raw"] = clip(raw_content)
            detail["rule"] = clip(rule_content)
            detail["final"] = clip(final_content)
            detail["content_truncated"] = any(
                len(text or "") > SHADOW_DETAIL_CONTENT_LIMIT
                for text in (raw_content, rule_content, final_content)
            )
            detail_path = os.path.join(directory, f"{SHADOW_DETAIL_PREFIX}-{detail['date']}.jsonl")
            with open(detail_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(detail, ensure_ascii=False) + "\n")
    except Exception:
        pass
