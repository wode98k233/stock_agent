#!/usr/bin/env python3
"""Stock Radar CLI 入口（TUI 工作台 / 旧式 REPL / 一次性命令）。

用法:
  python main.py                       启动 TUI 工作台
  python main.py --cli                 启动旧式 REPL
  python main.py "问题"                一次性提问（旧式）
  python main.py status --json         一次性命令
"""
import asyncio
import sys


async def _bootstrap_and_decide(force_cli: bool, one_shot_args: list[str] | None):
    """bootstrap 初始化后返回运行模式和上下文。"""
    from cli.bootstrap import bootstrap
    from cli.context import CLIContext

    ctx_dict = await bootstrap()
    ctx = CLIContext(
        agent_mode="react_stock",
        skill_register=ctx_dict.get("skill_register"),
        memory=ctx_dict.get("memory"),
        session_stats=ctx_dict.get("session_stats"),
        banner=ctx_dict.get("banner", ""),
    )
    # 注意：返回 (ctx_dict, ctx) 两个值是因为 TUI 和 CLI 使用不同的上下文体系。
    # TUI 从 ctx_dict 取值构建 boot 字典，CLI 使用 CLIContext 对象。
    # 两者互不兼容，暂时保留双返回值设计。
    return ctx_dict, ctx


async def _run_one_shot(ctx, one_shot_args: list[str]):
    """一次性命令模式。"""
    from cli.repl import handle_input

    cmd_input = "/" + " ".join(one_shot_args)
    result = await handle_input(cmd_input, ctx)
    if result.message:
        print(result.message)
    if result.data and isinstance(result.data, dict):
        summary = result.data.get("summary")
        if summary:
            print(summary)
    sys.exit(0 if result.ok else 1)


async def _run_cli_repl(ctx):
    """旧式 REPL 模式。"""
    from cli.repl import run_repl
    await run_repl(ctx)


def main(one_shot_args: list[str] | None = None, force_cli: bool = False):
    """主入口。

    Parameters
    ----------
    one_shot_args : list[str] | None
        非空时执行一次性命令而非进入交互模式。
    force_cli : bool
        强制使用旧式 CLI REPL（--cli 标志）。
    """
    if one_shot_args:
        # 一次性命令：bootstrap + 执行，都在 async 里
        async def _one_shot():
            ctx_dict, ctx = await _bootstrap_and_decide(force_cli, one_shot_args)
            await _run_one_shot(ctx, one_shot_args)
        asyncio.run(_one_shot())
    elif force_cli:
        # 旧式 REPL：bootstrap + REPL，都在 async 里
        async def _cli():
            ctx_dict, ctx = await _bootstrap_and_decide(force_cli, None)
            await _run_cli_repl(ctx)
        asyncio.run(_cli())
    else:
        # TUI 模式：bootstrap 在 async 里完成，TUI 在 sync 里启动
        # 因为 Textual App.run() 自己管理事件循环
        ctx_dict, ctx = asyncio.run(_bootstrap_and_decide(force_cli, None))
        boot = {
            "banner": ctx_dict.get("banner", ""),
            "skill_register": ctx_dict.get("skill_register"),
            "memory": ctx_dict.get("memory"),
            "session_stats": ctx_dict.get("session_stats"),
        }
        from cli.tui import run_tui
        run_tui(boot)


if __name__ == "__main__":
    args = sys.argv[1:]
    force_cli = "--cli" in args
    remaining = [a for a in args if a != "--cli"]
    main(one_shot_args=remaining or None, force_cli=force_cli)
