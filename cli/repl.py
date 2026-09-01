"""CLI REPL — 输入循环、命令分发、Agent 执行"""
from __future__ import annotations

import asyncio
import os
import traceback

from cli.context import CLIContext
from cli.parser import parse_input
from cli.commands import dispatch
from cli.commands.base import CommandResult
from cli.output import err


def _setup_readline():
    """初始化 readline 历史记录。"""
    try:
        import readline
        from utils.app_paths import get_app_dir
        history_file = os.path.join(get_app_dir(), ".cli_history")
        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass
        readline.set_history_length(200)
        # 注册退出时保存
        import atexit
        atexit.register(lambda: readline.write_history_file(history_file))
    except ImportError:
        pass  # Windows 没有 readline 时静默跳过


async def run_repl(ctx: CLIContext):
    """主 REPL 循环。"""
    _setup_readline()
    print(ctx.banner)

    while True:
        try:
            user_input = input("\n[>>>] ").strip()
            if not user_input:
                continue

            result = await handle_input(user_input, ctx)

            # 写操作确认流程
            if result.requires_confirm and result.pending_action:
                print(result.confirm_prompt)
                try:
                    confirm = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm = "cancel"
                if confirm in ("apply", "y", "yes", "确认"):
                    result = result.pending_action()
                else:
                    result = CommandResult(ok=True, message="[OK] 已取消。")

            should_exit = output_result(result)
            if should_exit:
                break

        except KeyboardInterrupt:
            from cli.banner import build_exit_stats
            print("\n" + build_exit_stats(ctx.session_stats))
            break
        except EOFError:
            break
        except Exception as e:
            print(err(f"{e}"))
            traceback.print_exc()


async def handle_input(user_input: str, ctx: CLIContext) -> CommandResult:
    """处理单次输入：解析 → 分发命令 或 执行 Agent。"""
    parsed = parse_input(user_input)

    if parsed.kind in ("command", "legacy"):
        # 尝试命令分发
        result = await dispatch(
            parsed.namespace, parsed.action, parsed.args, parsed.flags, ctx,
        )
        if result is not None:
            return result

    # 自然语言 → Agent 执行
    return await _execute_agent(user_input, ctx)


async def _execute_agent(user_input: str, ctx: CLIContext) -> CommandResult:
    """执行 Agent 分析。"""
    from utils.logger import get_logger
    from agents.factory import AgentFactory
    from tools.skills import SkillRegistry
    from config import Config

    logger, uuid, log_ctx, log_file = get_logger(user_input, console=True)
    progress_events = []

    def progress_callback(event):
        progress_events.append(event)
        _render_progress(event)

    try:
        agent = AgentFactory.get(ctx.agent_mode)
        registry = SkillRegistry(
            logger=logger,
            memory_mgr=ctx.memory,
            register=ctx.skill_register,
        )

        response = await agent.run(
            user_input,
            registry,
            ctx.memory,
            logger,
            progress_callback=progress_callback,
        )

        # 场景模式 fallback
        if not response and ctx.agent_mode == "scenario":
            agent = AgentFactory.get("react_stock")
            response = await agent.run(
                user_input, registry, ctx.memory, logger,
                progress_callback=progress_callback,
            )
            if response:
                ctx.agent_mode = "react_stock"

        # 记忆归档
        if response:
            try:
                from memory.sdk import get_sdk
                sdk = get_sdk()
                sdk.archive(user_input, response, None)
                logger.info("[archive] OK: query=%s count=%d", user_input[:50], sdk.backend.count())
            except Exception as e:
                logger.warning("[archive] FAIL: %s", e)

        # 累计统计
        if log_ctx and ctx.session_stats:
            ctx.session_stats.accumulate(log_ctx)

        # 结果摘要
        m = log_ctx.metrics if log_ctx else {}
        total_tokens = m.get('llm_tokens_in', 0) + m.get('llm_tokens_out', 0)
        cached = m.get('llm_cached_tokens', 0)
        cache_part = f"  cached: {cached:,}" if cached else ""
        summary = (
            f"\nlog: {log_file}\n"
            f"tokens: {total_tokens:,}{cache_part}  "
            f"llm_calls: {m.get('llm_calls', 0)}  "
            f"tools: {m.get('tool_calls', 0)}"
        )

        return CommandResult(
            ok=True,
            message=response or "分析完成，但未产生输出。",
            data={"uuid": uuid, "summary": summary},
        )

    except Exception as e:
        return CommandResult(ok=False, message=err(f"Agent 执行失败: {e}"))


_progress_t0 = 0.0


def _render_progress(event):
    """渲染进度事件。"""
    global _progress_t0
    if not isinstance(event, dict):
        return

    import time
    event_type = event.get("type", "")

    if event_type == "task_start":
        _progress_t0 = time.time()
        mode = event.get("mode", "")
        model = event.get("model", "")
        task_id = event.get("task_id", "")[:8]
        print(f"\n任务 {task_id} 开始，模式 {mode}  模型 {model}")

    elif event_type == "step_start":
        elapsed = time.time() - _progress_t0 if _progress_t0 else 0
        step = event.get("step", "")
        desc = event.get("description", "")[:30]
        print(f"  {elapsed:05.1f}s  {step:<20s}  running  {desc}")

    elif event_type == "step_complete":
        elapsed = time.time() - _progress_t0 if _progress_t0 else 0
        step = event.get("step", "")
        duration = event.get("duration", 0)
        status = event.get("status", "success")
        dur_str = f"{duration:.1f}s" if duration else ""
        print(f"  {elapsed:05.1f}s  {step:<20s}  {status:<8s} {dur_str}")

    elif event_type == "final":
        pass  # 最终结果由 handle_input 输出


def output_result(result: CommandResult) -> bool:
    """输出命令结果，返回是否应退出。"""
    if result.exit:
        from cli.banner import build_exit_stats
        # exit_stats 在 repl 层输出
        return True

    if result.message:
        print(result.message)

    # Agent 执行后的摘要
    if result.data and isinstance(result.data, dict):
        summary = result.data.get("summary")
        if summary:
            print(summary)

    return False
