"""
分析框架引擎 — 证据构建器

从 ExecutionState.tool_calls 构建 EvidenceBag（第一期为 dict）。
"""
import json
import logging
from typing import Any

from config import Config

logger = logging.getLogger(__name__)


def build_bag(tool_calls: list) -> dict:
    """
    从工具调用列表构建证据包。

    tool_calls: ExecutionState.tool_calls (ToolCall 对象列表)
    返回 EvidenceBag dict。
    """
    bag = {
        "items": [],
        "raw_text": "",
        "news_text": "",
        "json_fragments": [],
        "normalized_fields": {},
        "table_fragments": [],
        "metadata": {"tool_count": 0, "truncated_count": 0},
    }

    max_chars = Config.REPORT_MAX_TOOL_OUTPUT_CHARS

    for tc in tool_calls:
        # 兼容 ToolCall 对象和 dict 两种格式
        if isinstance(tc, dict):
            tool_name = tc.get("tool_name", "")
            tool_input = tc.get("tool_input", "")
            tool_output = tc.get("tool_output", "") or ""
        else:
            tool_name = getattr(tc, "tool_name", "")
            tool_input = getattr(tc, "tool_input", "")
            tool_output = getattr(tc, "tool_output", "") or ""

        truncated = False
        if len(tool_output) > max_chars:
            tool_output = tool_output[:max_chars]
            truncated = True
            bag["metadata"]["truncated_count"] += 1

        item = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "raw_output": tool_output,
            "output_preview": tool_output[:500],
            "output_length": len(tool_output),
            "parsed_json": None,
            "tables": [],
            "created_at": None,
            "truncated": truncated,
        }

        bag["raw_text"] += f"\n--- {tool_name} ---\n{tool_output}"

        # 处理 LangChain repr 格式: content='...' name='...' tool_call_id='...'
        clean_output = _clean_tool_output(tool_output)

        # mx_data 等工具返回 markdown 表格文本（非 JSON），解析表头→首行值
        _parse_markdown_tables(clean_output, bag)

        # 新闻类输出分离
        if _is_news_tool(tool_name, clean_output):
            max_news = Config.REPORT_MAX_NEWS_CHARS
            news_chunk = clean_output[:max_news] if len(clean_output) > max_news else clean_output
            bag["news_text"] += f"\n{news_chunk}"

        # JSON 解析（使用清理后的输出）
        try:
            parsed = json.loads(clean_output)
            item["parsed_json"] = parsed
            bag["json_fragments"].append({"tool": tool_name, "data": parsed})

            # 妙想 mx_data 表格解析
            _parse_mx_data(parsed, bag)
            # 已知技能输出解析
            _parse_known_skill(tool_name, parsed, bag)
        except (json.JSONDecodeError, TypeError):
            pass

        bag["items"].append(item)

    bag["metadata"]["tool_count"] = len(bag["items"])
    return bag


def build_bag_from_steps(step_results: list) -> dict:
    """
    从 PDOR step 结果构建证据包。

    step_results: [{"step": 1, "skill": "mx_data", "purpose": "...", "result": "..."}]
    每个 step 的 result 是 ReAct 子图的完整总结，包含表格、数据、分析。
    提取器的正则照样能从文本中匹配到 RSI、MACD、涨跌幅等数据。
    """
    bag = {
        "items": [],
        "raw_text": "",
        "news_text": "",
        "json_fragments": [],
        "normalized_fields": {},
        "table_fragments": [],
        "metadata": {"tool_count": 0, "truncated_count": 0},
    }

    max_chars = Config.REPORT_MAX_TOOL_OUTPUT_CHARS

    for sr in step_results:
        skill = sr.get("skill", "unknown")
        purpose = sr.get("purpose", "")
        result = sr.get("result", "") or ""

        truncated = False
        if len(result) > max_chars:
            result = result[:max_chars]
            truncated = True
            bag["metadata"]["truncated_count"] += 1

        item = {
            "tool_name": f"step:{skill}",
            "tool_input": purpose,
            "raw_output": result,
            "output_preview": result[:500],
            "output_length": len(result),
            "parsed_json": None,
            "tables": [],
            "created_at": None,
            "truncated": truncated,
        }

        bag["raw_text"] += f"\n--- Step [{skill}]: {purpose} ---\n{result}"

        # 新闻类 step 分离
        if _is_news_tool(skill, result):
            max_news = Config.REPORT_MAX_NEWS_CHARS
            news_chunk = result[:max_news] if len(result) > max_news else result
            bag["news_text"] += f"\n{news_chunk}"

        bag["items"].append(item)

    bag["metadata"]["tool_count"] = len(bag["items"])
    return bag


def _is_news_tool(tool_name: str, output: str) -> bool:
    """判断是否为新闻/搜索类工具。"""
    name_lower = tool_name.lower()
    if any(kw in name_lower for kw in ("search", "news", "新闻", "资讯")):
        return True
    return False


def _parse_markdown_tables(text: str, bag: dict):
    """解析 markdown 表格：表头列名 → 首个数据行的值，写入 normalized_fields。

    mx_data 返回的表格是按日期倒序（首行最新），因此只取第一个数据行；
    已有字段名不覆盖（保留首次出现的值）。
    """
    if not text:
        return
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < n:
            sep = lines[i + 1].strip()
            if sep.startswith("|") and any(c in sep for c in ("---", "===")):
                headers = [h.strip() for h in line.strip("|").split("|")]
                if i + 2 < n and lines[i + 2].strip().startswith("|"):
                    row = [c.strip() for c in lines[i + 2].strip().strip("|").split("|")]
                    for h, v in zip(headers, row):
                        if h and v and v != "-" and h not in bag["normalized_fields"]:
                            bag["normalized_fields"][h] = v
                i += 3
                continue
        i += 1


def _clean_tool_output(tool_output: str) -> str:
    """
    处理 LangChain repr 格式: content='...' name='...' tool_call_id='...'
    提取 content 字段的实际值。
    """
    if not tool_output:
        return tool_output

    # 如果已经是合法 JSON 或普通文本，直接返回
    stripped = tool_output.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        return tool_output

    # 处理 content='...' 格式
    if stripped.startswith("content='"):
        # 提取 content=' 和 ' name= 之间的内容
        try:
            start = len("content='")
            # 找到结束的 '，注意内容中可能有转义的 \'
            end = stripped.rfind("' name=")
            if end == -1:
                # 只有 content，没有 name
                end = stripped.rfind("'")
            if end > start:
                content = stripped[start:end]
                # 反转义
                content = content.replace("\\'", "'").replace("\\n", "\n")
                return content
        except Exception:
            pass

    # 处理 content="..." 格式
    if stripped.startswith('content="'):
        try:
            start = len('content="')
            end = stripped.rfind('" name=')
            if end == -1:
                end = stripped.rfind('"')
            if end > start:
                return stripped[start:end]
        except Exception:
            pass

    return tool_output


def _parse_mx_data(parsed: dict, bag: dict):
    """
    解析妙想 mx_data 返回的 dataTableDTOList/nameMap。
    将编码映射为中文名，写入 normalized_fields。
    """
    if not isinstance(parsed, dict):
        return

    _parse_mx_structured_preview(parsed, bag)

    # 检查是否是妙想数据格式
    data = parsed.get("data", parsed)
    if not isinstance(data, dict):
        return

    name_map = data.get("nameMap", {})
    table_list = data.get("dataTableDTOList", [])

    if name_map:
        # nameMap: {"f2": "最新价", "f3": "涨跌幅", ...}
        bag["normalized_fields"]["_mx_name_map"] = name_map

    for table_dto in table_list:
        if not isinstance(table_dto, dict):
            continue
        table_data = table_dto.get("table", [])
        if not isinstance(table_data, list):
            continue

        # 将表格行转为中文名索引
        for row in table_data:
            if not isinstance(row, dict):
                continue
            for code, value in row.items():
                cn_name = name_map.get(code, code)
                bag["normalized_fields"][cn_name] = value

        bag["table_fragments"].append({
            "source": "mx_data",
            "name_map": name_map,
            "rows": table_data,
        })


def _parse_mx_structured_preview(parsed: dict, bag: dict):
    """解析 mx 工具包装层返回的 tables/stocks 结构化预览。"""
    for table in parsed.get("tables", []) or []:
        rows = table.get("rows", []) if isinstance(table, dict) else []
        if not isinstance(rows, list):
            continue
        _merge_rows_into_normalized_fields(rows, bag)
        if rows:
            bag["table_fragments"].append({
                "source": "mx_structured_preview",
                "rows": rows,
            })

    stocks = parsed.get("stocks", [])
    if isinstance(stocks, list):
        _merge_rows_into_normalized_fields(stocks, bag)
        if stocks:
            bag["table_fragments"].append({
                "source": "mx_xuangu",
                "rows": stocks,
            })


def _merge_rows_into_normalized_fields(rows: list, bag: dict):
    """将结构化行中的字段平铺到 normalized_fields，保留首个非空值。"""
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if key not in bag["normalized_fields"] and value not in (None, ""):
                bag["normalized_fields"][key] = value


def _parse_known_skill(tool_name: str, parsed: dict, bag: dict):
    """
    解析已知内部技能的结构化输出。
    technical_analysis, valuation, sentiment_analysis 等。
    """
    if not isinstance(parsed, dict):
        return

    name_lower = tool_name.lower()

    # technical_analysis 技能
    if "technical" in name_lower or "技术" in name_lower:
        for key in ("rsi", "RSI", "macd", "MACD", "kdj", "KDJ", "ma", "MA"):
            if key in parsed:
                bag["normalized_fields"][key] = parsed[key]
        # 嵌套指标
        indicators = parsed.get("indicators", parsed.get("data", {}))
        if isinstance(indicators, dict):
            for k, v in indicators.items():
                bag["normalized_fields"][k] = v

    # valuation 技能
    if "valuation" in name_lower or "估值" in name_lower:
        for key in ("pe", "PE", "pb", "PB", "pe_percentile", "pb_percentile"):
            if key in parsed:
                bag["normalized_fields"][key] = parsed[key]
        data = parsed.get("data", {})
        if isinstance(data, dict):
            for k, v in data.items():
                bag["normalized_fields"][k] = v

    # sentiment_analysis 技能
    if "sentiment" in name_lower or "情绪" in name_lower:
        for key in ("sentiment", "score", "events"):
            if key in parsed:
                bag["normalized_fields"][key] = parsed[key]
