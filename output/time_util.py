"""
选股雷达 - 时间工具
确保所有查询和输出都有时间上下文
"""
from datetime import datetime
import re


# 需要注入时间上下文的关键词模式（用户没有显式提到时间时才注入）
TIME_PATTERNS = [
    r'今天', r'今日', r'昨天', r'昨日', r'前天',
    r'本周', r'上周', r'本月', r'上月',
    r'最近', r'近期', r'近\d+[天日周月年]',
    r'这[个一](?:周|月|年)',
    r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',  # 日期格式 2025-05-05
    r'\d{1,2}月\d{1,2}[日号]',
]


def has_explicit_time(text: str) -> bool:
    """检查用户输入是否包含显式的时间表述"""
    for pattern in TIME_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def enrich_with_time(user_input: str) -> str:
    """
    给用户输入注入当前时间上下文。
    如果用户没有提到时间，自动添加"当前时间"作为背景信息。
    """
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M")
    weekday_map = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
    weekday = weekday_map[now.weekday()]

    if has_explicit_time(user_input):
        # 用户提到了时间，只补充精确时间戳
        return (
            f"[背景信息]\n"
            f"- 当前时间：{time_str} {weekday}\n\n"
            f"[用户问题]\n{user_input}"
        )
    else:
        # 用户没有提到时间，默认注入"今天"
        return (
            f"[背景信息]\n"
            f"- 当前时间：{time_str} {weekday}\n"
            f"- 用户未指定时间，默认查询最新/今日数据\n\n"
            f"[用户问题]\n{user_input}"
        )


def get_data_timestamp() -> str:
    """获取数据获取时间戳，用于输出中标注数据时效"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_news_time(time_str: str) -> str:
    """
    格式化新闻时间，确保输出中包含时间信息。
    处理各种常见的时间格式。
    """
    if not time_str:
        return "时间未知"
    # 已经是完整格式的直接返回
    if re.match(r'\d{4}-\d{2}-\d{2}', time_str):
        return time_str
    # 只有日期没有年份的，补全年份
    if re.match(r'\d{1,2}-\d{1,2}', time_str):
        return f"{datetime.now().year}-{time_str}"
    return time_str
