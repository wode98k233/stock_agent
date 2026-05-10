"""
选股雷达 - CLI 输出格式化器
提供结构化、对齐、易读的终端输出
"""
from datetime import datetime


# ── 基础组件 ──────────────────────────────────────────────

def section_header(title: str, subtitle: str = "", width: int = 56) -> str:
    """生成区块标题"""
    sep = "━" * width
    lines = [f"\n  {sep}"]
    if subtitle:
        lines.append(f"    {title} | {subtitle}")
    else:
        lines.append(f"    {title}")
    lines.append(f"  {sep}")
    return "\n".join(lines)


def sub_header(title: str) -> str:
    """生成子标题"""
    return f"\n  【{title}】"


def divider(width: int = 56) -> str:
    """生成分隔线"""
    return f"  {'─' * width}"


def kv_line(key: str, value: str, key_width: int = 10) -> str:
    """生成键值对行"""
    return f"  {key:<{key_width}} {value}"


def text_block(text: str, indent: int = 2) -> str:
    """生成缩进文本块"""
    prefix = " " * indent
    lines = text.strip().split("\n")
    return "\n".join(f"{prefix}{line}" for line in lines)


def note(text: str) -> str:
    """生成注释/提示行"""
    return f"  ℹ {text}"


def data_time_tag(timestamp: str) -> str:
    """生成数据时间标签"""
    return f"\n  ── 数据获取时间: {timestamp} ──"


def news_item(index: int, title: str, time_str: str, source: str = "") -> str:
    """生成新闻条目"""
    parts = [f"  {index}. {title}"]
    meta = []
    if time_str:
        meta.append(time_str)
    if source:
        meta.append(source)
    if meta:
        parts[0] += f"  ({' | '.join(meta)})"
    return parts[0]


# ── 表格渲染 ──────────────────────────────────────────────

def ascii_table(headers: list, rows: list, col_widths: list = None) -> str:
    """
    渲染 ASCII 表格

    Args:
        headers: 列标题列表
        rows: 数据行列表，每行是与headers等长的列表
        col_widths: 列宽列表，None则自动计算
    """
    if not rows:
        return "  (无数据)"

    if col_widths is None:
        col_widths = []
        for i, header in enumerate(headers):
            max_w = len(header)
            for row in rows:
                if i < len(row):
                    cell_len = len(str(row[i]))
                    if cell_len > max_w:
                        max_w = cell_len
            col_widths.append(min(max_w + 2, 20))

    def format_row(cells, widths):
        parts = []
        for i, cell in enumerate(cells):
            w = widths[i] if i < len(widths) else 12
            text = str(cell) if cell is not None else ""
            # 中文字符宽度补偿
            cjk_count = sum(1 for c in text if '一' <= c <= '鿿')
            display_len = len(text) + cjk_count
            padding = max(0, w - display_len)
            parts.append(text + " " * padding)
        return "  " + " ".join(parts)

    lines = []
    lines.append(format_row(headers, col_widths))
    lines.append("  " + " ".join("─" * w for w in col_widths))
    for row in rows:
        lines.append(format_row(row, col_widths))
    return "\n".join(lines)


# ── 场景专用渲染 ──────────────────────────────────────────

def render_screening_result(
    board_name: str,
    conditions: str,
    stocks: list,
    data_timestamp: str,
    max_display: int = 10,
) -> str:
    """
    渲染条件选股结果

    Args:
        board_name: 板块名
        conditions: 筛选条件描述
        stocks: 筛选后的股票列表 [{"code", "name", "price", "pct_chg", "pe", "extra"}]
        data_timestamp: 数据获取时间
        max_display: 最多显示条数
    """
    subtitle = f"{board_name} · {conditions}" if board_name else conditions
    lines = [section_header("条件选股结果", subtitle)]

    if not stocks:
        lines.append("\n  未找到符合条件的股票。建议放宽筛选条件重试。")
        lines.append(data_time_tag(data_timestamp))
        return "\n".join(lines)

    display_stocks = stocks[:max_display]
    headers = ["#", "代码", "名称", "现价", "涨幅", "PE"]
    rows = []
    for i, s in enumerate(display_stocks, 1):
        pct = s.get("pct_chg", 0)
        pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
        pe = s.get("pe", 0)
        pe_str = f"{pe:.1f}" if pe and pe > 0 else "-"
        rows.append([
            str(i),
            s.get("code", ""),
            s.get("name", ""),
            f"{s.get('price', 0):.2f}",
            pct_str,
            pe_str,
        ])

    lines.append("")
    lines.append(ascii_table(headers, rows, [4, 8, 10, 10, 10, 8]))

    total = len(stocks)
    if total > max_display:
        lines.append(f"\n  共筛选出 {total} 只，显示前 {max_display} 只")
    else:
        lines.append(f"\n  共筛选出 {total} 只符合条件的股票")

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)


def render_stock_analysis(
    stock_name: str,
    stock_code: str,
    sections: list,
    data_timestamp: str,
) -> str:
    """
    渲染个股分析结果

    Args:
        stock_name: 股票名称
        stock_code: 股票代码
        sections: [{"title": "技术面", "content": "..."}]
        data_timestamp: 数据获取时间
    """
    lines = [section_header("个股分析", f"{stock_name} ({stock_code})")]

    for section in sections:
        lines.append(sub_header(section["title"]))
        lines.append(text_block(section["content"]))

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)


def render_sector_analysis(
    sector_name: str,
    sector_quote: dict,
    top_stocks: list,
    driver_analysis: str,
    data_timestamp: str,
    max_stocks: int = 5,
) -> str:
    """
    渲染板块分析结果

    Args:
        sector_name: 板块名
        sector_quote: 板块行情 {"pct_chg", "amount", "pct_chg_5d"}
        top_stocks: 龙头股列表
        driver_analysis: LLM生成的驱动因素分析
        data_timestamp: 数据获取时间
    """
    lines = [section_header("板块分析", sector_name)]

    # 板块行情
    lines.append(sub_header("板块行情"))
    pct = sector_quote.get("pct_chg", 0)
    pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
    pct5 = sector_quote.get("pct_chg_5d", 0)
    pct5_str = f"+{pct5:.2f}%" if pct5 >= 0 else f"{pct5:.2f}%"
    amount = sector_quote.get("amount", 0)
    amount_str = f"{amount / 1e8:.0f}亿" if amount > 10000 else f"{amount:.0f}"
    lines.append(kv_line("今日涨幅", pct_str))
    lines.append(kv_line("5日涨幅", pct5_str))
    lines.append(kv_line("成交额", amount_str))

    # 龙头股
    if top_stocks:
        lines.append(sub_header("龙头股"))
        display = top_stocks[:max_stocks]
        headers = ["#", "代码", "名称", "涨幅", "近5日"]
        rows = []
        for i, s in enumerate(display, 1):
            p = s.get("pct_chg", 0)
            p_str = f"+{p:.2f}%" if p >= 0 else f"{p:.2f}%"
            p5 = s.get("pct_chg_5d", 0)
            p5_str = f"+{p5:.2f}%" if p5 >= 0 else f"{p5:.2f}%"
            rows.append([str(i), s.get("code", ""), s.get("name", ""), p_str, p5_str])
        lines.append("")
        lines.append(ascii_table(headers, rows, [4, 8, 10, 10, 10]))

    # 驱动因素分析
    if driver_analysis:
        lines.append(sub_header("驱动因素分析"))
        lines.append(text_block(driver_analysis))

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)


def render_market_overview(
    index_data: list,
    breadth: dict,
    hot_sectors: list,
    cold_sectors: list,
    summary: str,
    data_timestamp: str,
) -> str:
    """
    渲染市场概览

    Args:
        index_data: [{"name", "price", "pct_chg", "amount"}]
        breadth: {"up", "down", "flat", "limit_up", "limit_down"}
        hot_sectors: 涨幅前5板块
        cold_sectors: 跌幅前5板块
        summary: LLM生成的简评
        data_timestamp: 数据获取时间
    """
    lines = [section_header("市场概览", datetime.now().strftime("%Y-%m-%d"))]

    # 大盘指数
    if index_data:
        lines.append(sub_header("大盘指数"))
        headers = ["指数", "点位", "涨幅", "成交额"]
        rows = []
        for idx in index_data:
            pct = idx.get("pct_chg", 0)
            pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
            amt = idx.get("amount", 0)
            amt_str = f"{amt / 1e8:.0f}亿" if amt > 10000 else f"{amt:.0f}"
            rows.append([idx.get("name", ""), f"{idx.get('price', 0):.2f}", pct_str, amt_str])
        lines.append("")
        lines.append(ascii_table(headers, rows, [10, 10, 10, 10]))

    # 涨跌统计
    if breadth:
        lines.append(sub_header("涨跌统计"))
        up = breadth.get("up", 0)
        down = breadth.get("down", 0)
        limit_up = breadth.get("limit_up", 0)
        limit_down = breadth.get("limit_down", 0)
        ratio = f"{up / down:.1f}:1" if down > 0 else f"{up}:0"
        lines.append(f"  上涨 {up} 家  下跌 {down} 家  涨停 {limit_up} 家  跌停 {limit_down} 家")
        mood = "偏多" if up > down * 1.5 else ("偏空" if down > up * 1.5 else "震荡")
        lines.append(f"  涨跌比 {ratio}  市场情绪{mood}")

    # 热点板块
    if hot_sectors:
        lines.append(sub_header("涨幅前5板块"))
        for i, s in enumerate(hot_sectors[:5], 1):
            pct = s.get("pct_chg", 0)
            pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
            lines.append(f"  {i}. {s.get('name', '')}({pct_str})")

    if cold_sectors:
        lines.append(sub_header("跌幅前5板块"))
        for i, s in enumerate(cold_sectors[:5], 1):
            pct = s.get("pct_chg", 0)
            pct_str = f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"
            lines.append(f"  {i}. {s.get('name', '')}({pct_str})")

    # 简评
    if summary:
        lines.append(sub_header("简评"))
        lines.append(text_block(summary))

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)


def render_comparison(
    stock_a_name: str,
    stock_b_name: str,
    metrics: list,
    conclusion: str,
    data_timestamp: str,
) -> str:
    """
    渲染对比分析

    Args:
        stock_a_name: 股票A名称
        stock_b_name: 股票B名称
        metrics: [{"label": "现价", "a": "28.50", "b": "8.20"}]
        conclusion: LLM生成的对比结论
        data_timestamp: 数据获取时间
    """
    lines = [section_header("对比分析", f"{stock_a_name} vs {stock_b_name}")]

    if metrics:
        label_w = max(len(m.get("label", "")) for m in metrics) + 4
        a_w = max(len(str(m.get("a", ""))) for m in metrics) + 2
        b_w = max(len(str(m.get("b", ""))) for m in metrics) + 2

        lines.append(f"\n  {'指标':<{label_w}} {stock_a_name:<{a_w}} {stock_b_name:<{b_w}}")
        lines.append(f"  {'─' * label_w} {'─' * a_w} {'─' * b_w}")
        for m in metrics:
            lines.append(f"  {m.get('label', ''):<{label_w}} {str(m.get('a', '')):<{a_w}} {str(m.get('b', '')):<{b_w}}")

    if conclusion:
        lines.append(sub_header("对比结论"))
        lines.append(text_block(conclusion))

    lines.append(data_time_tag(data_timestamp))
    return "\n".join(lines)
