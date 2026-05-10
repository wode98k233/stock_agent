"""
选股雷达 - Agent 工具函数
包含 Agent 相关的通用工具函数
"""
import sys
from config import Config


def _is_abnormal_result(text: str) -> bool:
    """检测 Agent 返回的异常结果

    只检测明确的异常模式，避免误报正常 JSON 输出。
    """
    if not text or not text.strip():
        return True
    stripped = text.strip()
    # 只检测明确的异常提示，不检测 JSON 结构字段（如 "code"）
    abnormal_patterns = [
        'Sorry, need more steps',
        'Agent stopped due to max iterations',
    ]
    for pattern in abnormal_patterns:
        if pattern.lower() in stripped.lower():
            return True
    if len(stripped) < 20:
        return True
    # 空数据模式 — 只检查前 200 字符
    empty_data_patterns = [
        "无数据", "无 dataTableDTOList", "接口返回中无",
        "查询结果为空", "暂无数据",
    ]
    check_text = stripped[:200].lower()
    for pattern in empty_data_patterns:
        if pattern in check_text:
            return True
    return False


def _error_fingerprint(e: Exception) -> str:
    """生成错误指纹，用于判断是否为同类错误"""
    error_type = type(e).__name__
    error_msg = str(e)[:100]
    if 'code' in error_msg.lower():
        return f"{error_type}:code_related"
    elif 'timeout' in error_msg.lower():
        return f"{error_type}:timeout"
    elif 'recursion' in error_msg.lower() or 'more steps' in error_msg.lower():
        return f"{error_type}:recursion_limit"
    elif 'json' in error_msg.lower():
        return f"{error_type}:json_parse"
    else:
        return f"{error_type}:{error_msg[:50]}"


def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_set = set(a)
    b_set = set(b)
    if not a_set or not b_set:
        return 0.0
    intersection = len(a_set & b_set)
    union = len(a_set | b_set)
    jaccard = (intersection / union) if union > 0 else 0.0
    len_ratio = (min(len(a), len(b)) / max(len(a), len(b))) if max(len(a), len(b)) > 0 else 0.0
    return jaccard * (0.7 + 0.3 * len_ratio)


def _extract_keywords(text: str) -> set:
    """从文本中提取关键词（中文字符 + 英文单词 + 数字）"""
    import re
    if not text:
        return set()
    chinese_chars = set(re.findall(r'[一-鿿]', text))
    english_words = set(w.lower() for w in re.findall(r'[a-zA-Z]+', text) if len(w) > 1)
    return chinese_chars | english_words


def _keyword_overlap(a: str, b: str) -> float:
    """
    基于关键词集合计算两段文本的重叠率
    比 _text_similarity 更能识别语义重复
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    kw_a = _extract_keywords(a)
    kw_b = _extract_keywords(b)

    if not kw_a or not kw_b:
        return 0.0

    intersection = len(kw_a & kw_b)
    min_size = min(len(kw_a), len(kw_b))

    return intersection / min_size if min_size > 0 else 0.0


async def _generate_step_summary(step_result: str, step_purpose: str, llm, logger) -> dict:
    """LLM 生成单步结论的结构化摘要"""
    from agents.prompts import STEP_SUMMARY_PROMPT
    from utils.llm_factory import llm_json_with_retry

    prompt = STEP_SUMMARY_PROMPT.replace("{purpose}", step_purpose).replace("{result}", step_result)

    messages = [
        ("system", "你是一个数据分析师，擅长从执行结果中提取关键信息。"),
        ("user", prompt),
    ]

    try:
        result = llm_json_with_retry(llm, messages, logger, label="step-summary")
        if result:
            return result
    except Exception as e:
        logger.warning("E", f"生成步骤摘要失败: {e}")

    return {}


def _confirm_step_execution(step_num, skill_name, purpose, instruction, logger):
    if Config.LOG_LEVEL != 'DEBUG' or not Config.DEBUG_STEP_CONFIRM:
        return True
    if not sys.stdin.isatty():
        return True

    print(f"\n{'='*60}")
    print(f"🔍【调试模式】即将执行 Step {step_num}")
    print(f"{'='*60}")
    print(f"技能：{skill_name}")
    print(f"目的：{purpose}")
    print(f"指令：{instruction}")
    print(f"{'='*60}")

    while True:
        try:
            confirm = input("是否继续执行？(y/yes 确认，其他取消): ").strip().lower()
            if confirm in ['y', 'yes']:
                print("✅ 继续执行...")
                return True
            else:
                print("❌ 取消执行")
                return False
        except (KeyboardInterrupt, EOFError):
            print("\n❌ 取消执行")
            return False
