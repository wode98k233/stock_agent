"""
推荐问题池生成脚本
调用 LLM 生成一批推荐问题，追加/替换到 data/suggested_questions.json
用法: python scripts/generate_questions.py [--count 30] [--replace]
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.app_paths import get_questions_pool_path

PROMPT_TEMPLATE = """你是一个专业的A股投资顾问助手。请生成 {count} 个用户可能会问的股票分析问题。

要求：
1. 问题覆盖以下维度（每个维度至少2个问题）：
   - 板块分析（如：新能源、半导体、医药、军工、白酒等）
   - 技术面选股（如：金叉、底背离、放量突破等）
   - 基本面筛选（如：低PE、高ROE、高分红等）
   - 资金流向（如：北向资金、融资余额、大宗交易等）
   - 市场情绪（如：涨跌比、大盘走势、成交额等）
   - 个股分析（如：茅台、比亚迪等知名个股）
   - 宏观政策（如：降息、产业政策等）
2. 问题要口语化、自然，像真实用户会问的
3. 不要重复或过于相似的问题

请严格输出以下JSON格式，不要包含其他文字：
```json
{{
  "questions": [
    {{"text": "问题内容", "tags": ["标签1", "标签2"]}}
  ]
}}
```"""


def load_pool(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "updated_at": "", "questions": []}


def save_pool(path: str, pool: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def generate_questions(count: int) -> list[dict]:
    from utils.llm_factory import get_llm, llm_json_with_retry
    from utils.logger import get_logger

    logger, _, _ = get_logger("generate_questions")
    llm = get_llm()

    messages = [("user", PROMPT_TEMPLATE.format(count=count))]
    result = llm_json_with_retry(llm, messages, logger, label="generate_questions")

    if result and "questions" in result:
        return result["questions"]
    return []


def main():
    parser = argparse.ArgumentParser(description="生成推荐问题池")
    parser.add_argument("--count", type=int, default=30, help="生成问题数量")
    parser.add_argument("--replace", action="store_true", help="替换而非追加")
    args = parser.parse_args()

    path = get_questions_pool_path()
    pool = load_pool(path)

    print(f"🔄 正在调用 LLM 生成 {args.count} 条推荐问题...")
    new_questions = generate_questions(args.count)

    if not new_questions:
        print("❌ 生成失败，请检查 LLM 配置")
        sys.exit(1)

    if args.replace:
        pool["questions"] = new_questions
    else:
        existing_texts = {q["text"] for q in pool["questions"]}
        added = [q for q in new_questions if q["text"] not in existing_texts]
        pool["questions"].extend(added)
        print(f"✅ 新增 {len(added)} 条（去重后），已有 {len(existing_texts)} 条")

    from datetime import datetime
    pool["updated_at"] = datetime.now().strftime("%Y-%m-%d")

    save_pool(path, pool)
    print(f"💾 已保存到 {path}，共 {len(pool['questions'])} 条问题")


if __name__ == "__main__":
    main()
