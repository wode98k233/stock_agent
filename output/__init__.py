"""
选股雷达 - 输出模块
提供结构化输出格式化和时间工具
"""
from output.formatter import (
    section_header, sub_header, divider, kv_line,
    text_block, note, data_time_tag, ascii_table,
    render_screening_result, render_stock_analysis,
    render_sector_analysis, render_market_overview,
    render_comparison, news_item,
)
from output.time_util import (
    enrich_with_time, has_explicit_time,
    get_data_timestamp, format_news_time,
)
