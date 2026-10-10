"""澄清反问的对话消息文本。

路由落库（用户正常作答）与超时兜底（无人作答按推荐继续）两条路径共用，
避免问答文案在两处各写一份、久之漂移。
"""


def format_clarify_questions(questions: list) -> str:
    lines = ["**开工前，我想先确认几个信息：**"]
    for i, q in enumerate(questions or [], 1):
        lines.append(f"\n{i}. {q.get('question', '')}")
        for opt in q.get("options") or []:
            mark = "（推荐）" if opt.get("value") == q.get("recommended") else ""
            lines.append(f"   - {opt.get('label', '')}{mark}")
    return "\n".join(lines)


def format_clarify_answers(questions: list, answers: dict, skipped=None) -> str:
    """作答文本：跳过的题显式记「（跳过）」，其余按 label 展示。

    跳过 ≠ 未出现：历史里要能看出「这题问了但用户没答」，
    否则回看对话会以为 AI 根本没问。
    """
    from agents.shared.clarify import resolve_answer_text

    skip = {str(s) for s in (skipped or [])}
    parts = []
    for q in questions or []:
        qid = str(q.get("id"))
        if qid in skip:
            parts.append(f"{q.get('question', '')}：（跳过）")
            continue
        a = resolve_answer_text(q, (answers or {}).get(qid))
        if a:
            parts.append(f"{q.get('question', '')}：{a}")
    return "补充信息：" + "；".join(parts) if parts else "（未补充）"
