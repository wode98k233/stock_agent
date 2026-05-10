"""
选股雷达 - 场景输出格式化器

提供统一的格式化函数，确保所有场景输出风格一致。
"""
from output.formatter import section_header, sub_header, data_time_tag, kv_line
from agents.scenarios.common import format_amount, format_pct, format_market_cap


# ── 中文对齐辅助 ──────────────────────────────────────────

def _display_width(text: str) -> int:
    """计算字符串的显示宽度（中文字符占2个英文字符宽度）"""
    width = 0
    for ch in text:
        if '一' <= ch <= '鿿' or '　' <= ch <= '〿' or '＀' <= ch <= '￯':
            width += 2
        else:
            width += 1
    return width


def _pad_right(text: str, target_width: int) -> str:
    """右填充到目标显示宽度（考虑中文字符宽度）"""
    current = _display_width(text)
    padding = max(0, target_width - current)
    return text + " " * padding


# ── 对比表格 ──────────────────────────────────────────────

def format_comparison_table(stocks_data: list, metrics: list) -> str:
    """
    格式化对比表格
    stocks_data: [(stock_dict, bundle_dict, tech_dict), ...]
    metrics: [{"label": str, "values": [str, ...]}, ...]
    """
    if not stocks_data or not metrics:
        return ""

    names = [f"{s['name']}" for s, _, _ in stocks_data]

    # 计算列宽：取所有列名和数据值的最大显示宽度
    all_texts = list(names)
    for m in metrics:
        all_texts.extend([m["label"]] + m["values"])
    col_width = max(12, max(_display_width(t) for t in all_texts) + 2)

    # 表头
    header = _pad_right("指标", col_width) + "".join(_pad_right(n, col_width) for n in names)
    separator = "─" * _display_width(header)

    rows = [header, separator]
    for m in metrics:
        row = _pad_right(m["label"], col_width) + "".join(_pad_right(v, col_width) for v in m["values"])
        rows.append(row)

    rows.append(separator)
    return "\n".join(rows)
