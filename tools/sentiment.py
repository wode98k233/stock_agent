"""
选股雷达 - 新闻情感分析
支持: 个股新闻、板块新闻、关键词新闻
量化打分 -10(极大利空) ~ +10(极大利好)
短期/长期影响分别评估
支持时间衰减权重
"""
import json
import logging
from datetime import datetime, timedelta
from utils.llm_factory import get_llm, tracked_invoke, llm_json_with_retry
from utils.memory import MemoryManager

SYSTEM_PROMPT = """你是专业财经新闻情感分析师。分析新闻对股票/板块的影响。

输出严格 JSON（不要任何其他文字）:
{
  "news_analysis": [
    {
      "news_index": 1,
      "title": "新闻标题",
      "score": -10到10的数字,
      "impact": "short" 或 "long" 或 "both",
      "reason": "打分原因，50字以内"
    }
  ],
  "total_score": 所有新闻综合分数,
  "conclusion": "利好" 或 "利空" 或 "中性",
  "summary": "整体结论，100字以内"
}

评分标准:
- 10: 重大利好（如重大合同、业绩超预期大增）
- 5~9: 一般利好（如政策支持、行业利好）
- 1~4: 轻微利好
- 0: 中性
- -1~-4: 轻微利空
- -5~-9: 一般利空（如业绩下滑、监管处罚）
- -10: 重大利空（如暴雷、重大诉讼）

注意:
- 区分短期影响和长期影响
- 如有两个矛盾新闻要综合判断（如原材料涨价短期利好但长期利空+毁约大利空→综合利空）
- 分数必须是数字
- 根据新闻的时效性权重调整影响力评估：权重高的新闻（近期）影响力更大，权重低的新闻（较久远）影响力应相应降低
"""


def _calc_recency_weight(time_str: str) -> tuple:
    """
    计算新闻的时间衰减权重
    :return: (权重, 描述文本)
    """
    if not time_str:
        return 0.5, '未知时间'

    try:
        # 尝试多种时间格式
        now = datetime.now()
        news_time = None

        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d', '%Y/%m/%d']:
            try:
                news_time = datetime.strptime(str(time_str).strip(), fmt)
                break
            except ValueError:
                continue

        if news_time is None:
            # 尝试解析相对时间（如"3小时前"、"昨天"）
            time_str_lower = str(time_str).lower()
            if '小时' in time_str_lower or '分钟' in time_str_lower:
                return 1.0, '今天'
            if '天前' in time_str_lower:
                import re
                m = re.search(r'(\d+)', time_str_lower)
                if m:
                    days = int(m.group(1))
                    if days <= 1: return 1.0, '1天内'
                    if days <= 3: return 0.8, f'{days}天前'
                    if days <= 7: return 0.5, f'{days}天前'
                    if days <= 30: return 0.3, f'{days}天前'
                    return 0.1, f'{days}天前'
            return 0.5, '时间解析失败'

        days_ago = (now - news_time).days
        if days_ago <= 1: return 1.0, '1天内'
        if days_ago <= 3: return 0.8, f'{days_ago}天前'
        if days_ago <= 7: return 0.5, f'{days_ago}天前'
        if days_ago <= 30: return 0.3, f'{days_ago}天前'
        return 0.1, f'{days_ago}天前'

    except Exception:
        return 0.5, '时间解析异常'


def analyze_sentiment(news_list: list, memory_mgr: MemoryManager = None, logger=None) -> dict:
    """
    分析新闻列表的情感
    :param news_list: [{'title':..., 'time':..., 'content':..., 'source':...}, ...]
    :param memory_mgr: MemoryManager，分析时临时关闭以节省 token
    :param logger: 可选的 logger 对象
    :return: {'news_analysis': [...], 'total_score': float, 'conclusion': str, 'summary': str}
    """
    if not news_list:
        return {'news_analysis': [], 'total_score': 0, 'conclusion': '中性', 'summary': '无新闻数据'}

    # 使用传入的 logger，如果没有则创建一个简单的
    if logger is None:
        from utils.logger import get_child_logger
        logger = get_child_logger("sentiment")

    # 构建新闻文本（带时间衰减权重）
    lines = []
    for i, n in enumerate(news_list):
        title = n.get('title', '')
        time_str = n.get('time', '')
        content = n.get('content', '')
        weight, time_desc = _calc_recency_weight(time_str)
        lines.append(f"{i+1}. [{time_desc}] {title} (权重:{weight})\n   {content}")
    news_text = "\n".join(lines)

    llm = get_llm()
    messages = [
        ("system", SYSTEM_PROMPT),
        ("user", f"请分析以下新闻:\n\n{news_text}")
    ]

    # 临时关闭记忆（新闻分析不需要记录到对话历史）
    if memory_mgr:
        memory_mgr.disable()
    try:
        result = llm_json_with_retry(llm, messages, logger, label="sentiment")
        if result and 'total_score' in result:
            logger.info(f"情感分析完成 | 总分: {result['total_score']} | 结论: {result.get('conclusion', '?')}")
            return result
        # 降级
        return {
            'news_analysis': [],
            'total_score': 0,
            'conclusion': '中性',
            'summary': '情感分析解析失败' if result else '情感分析无返回'
        }
    finally:
        if memory_mgr:
            memory_mgr.enable()


def merge_sentiment(results: list) -> dict:
    """
    合并多个情感分析结果（如个股+板块+概念新闻各自分析后合并）
    :param results: 多个 analyze_sentiment 的返回值
    :return: 合并后的情感分析
    """
    if not results:
        return {'total_score': 0, 'conclusion': '中性', 'summary': '无数据'}

    all_analysis = []
    total = 0
    count = 0
    for r in results:
        if isinstance(r, dict):
            all_analysis.extend(r.get('news_analysis', []))
            total += r.get('total_score', 0)
            count += 1

    avg = total / count if count > 0 else 0
    if avg > 2:
        conclusion = '利好'
    elif avg < -2:
        conclusion = '利空'
    else:
        conclusion = '中性'

    return {
        'news_analysis': all_analysis,
        'total_score': round(avg, 2),
        'conclusion': conclusion,
        'summary': f"综合{count}个来源的情感分析，平均分{avg:.1f}，整体{conclusion}"
    }
