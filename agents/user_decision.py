"""
选股雷达 - 统一用户决策交互
所有 Agent 遇到需要用户决策的场景，统一使用此模块

支持两种模式：
- CLI 模式：直接 print/input 交互
- Web 模式：通过 budget_decision_ctx 回调走 SSE 通道
"""


def ask_user_decision(header: str, status_lines: list[str], options: list[str]) -> str:
    """
    打印决策信息 + 编号选项，返回用户选择

    自动检测 Web 模式（通过 budget_decision_ctx），Web 模式下通过回调返回。

    Args:
        header: 决策场景标题（如 "Step 3 执行失败：baostock 接口超时"）
        status_lines: 当前状态描述行（已获取/缺失数据等）
        options: 选项列表（如 ["跳过此步，继续执行后续步骤", "基于已有数据生成总结"]）

    Returns:
        用户选择的选项文本，或 "custom" 表示用户输入了自定义内容
    """
    # Web 模式：通过 SSE 回调让用户决策
    try:
        from agents.shared.budget_ctx import budget_decision_ctx
        web_callback = budget_decision_ctx.get()
        if web_callback is not None:
            decision = web_callback({
                "reason": "user_decision",
                "header": header,
                "status_lines": status_lines,
                "options": options,
            })
            return decision or (options[-1] if options else "cancel")
    except Exception:
        pass

    # CLI 模式：交互式 print/input
    print(f"\n{'='*60}")
    print(f"⚠️ {header}")
    print(f"{'='*60}")

    for line in status_lines:
        print(f"  {line}")

    # 构建快捷提示（对齐 ReAct 交互风格）
    confirm_hint = options[0] if options else "确认"
    reject_hint = options[-1] if len(options) > 1 else "取消"
    print(f"\n请选择 (y/{confirm_hint}，n/{reject_hint}，或输入编号):")

    while True:
        try:
            choice = input("> ").strip()
            if not choice:
                continue

            lower = choice.lower()
            if lower in ('y', 'yes'):
                selected = options[0]
                print(f"✅ 已选择: {selected}")
                return selected
            if lower in ('n', 'no'):
                selected = options[-1]
                print(f"✅ 已选择: {selected}")
                return selected

            idx = int(choice)
            if 1 <= idx <= len(options):
                selected = options[idx - 1]
                print(f"✅ 已选择: {selected}")
                return selected
            else:
                print(f"请输入 y/n 或 1-{len(options)} 之间的数字")
        except ValueError:
            print(f"✅ 已收到你的输入: {choice}")
            return "custom"
        except (KeyboardInterrupt, EOFError):
            print("\n❌ 取消操作")
            return options[-1] if options else "cancel"


def format_step_status(plan: list, past_steps: list, current_step: int) -> tuple[list[str], list[str]]:
    """
    格式化计划执行状态，用于决策展示

    Returns:
        (status_lines, missing_lines): 已获取和缺失的数据描述
    """
    status_lines = []
    missing_lines = []

    for i, step in enumerate(plan):
        step_num = step.get('step', i + 1)
        purpose = step.get('purpose', '')
        skill = step.get('skill', '')

        # 检查是否已完成
        completed = False
        for desc, _ in past_steps:
            if f"Step {step_num}" in desc or purpose in desc:
                completed = True
                break

        if completed:
            status_lines.append(f"✅ Step {step_num}: [{skill}] {purpose}")
        elif i == current_step:
            status_lines.append(f"🔄 Step {step_num}: [{skill}] {purpose} (当前)")
        else:
            missing_lines.append(f"❌ Step {step_num}: [{skill}] {purpose}")

    return status_lines, missing_lines
