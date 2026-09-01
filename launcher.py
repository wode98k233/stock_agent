"""选股雷达统一打包入口。

打包后推荐用法：
  stock-radar.exe cli
  stock-radar.exe web
  stock-radar.exe trace serve -p 8765

不传子命令时默认进入 CLI，兼容原有使用习惯。
"""
from pathlib import Path
import sys


def _configure_console():
    from utils.console import configure_console

    configure_console()


def _default_command() -> str:
    exe_name = Path(sys.argv[0]).stem.lower()
    if exe_name in {"stock-radar-web", "stock-radar-server"}:
        return "web"
    if exe_name in {"stock-radar-trace", "stock-radar-agent-trace", "stock-radar-agent_trace"}:
        return "trace"
    return "cli"


def _resolve_command() -> tuple[str, list[str]]:
    explicit_commands = {"cli", "web", "server", "trace", "agent-trace", "agent_trace", "-h", "--help", "help"}
    default = _default_command()
    if len(sys.argv) == 1:
        return default, []

    first_arg = sys.argv[1].lower()
    if first_arg in explicit_commands:
        return first_arg, sys.argv[2:]

    if default != "cli":
        return default, sys.argv[1:]

    return first_arg, sys.argv[2:]


def _run_cli(args: list[str] | None = None):
    from main import main as cli_main

    # 检测 --cli 标志
    force_cli = False
    filtered_args = list(args) if args else None
    if filtered_args and "--cli" in filtered_args:
        force_cli = True
        filtered_args.remove("--cli")
        if not filtered_args:
            filtered_args = None

    cli_main(one_shot_args=filtered_args, force_cli=force_cli)


def _run_web():
    from server.__main__ import main as web_main

    web_main()


def _run_trace():
    from utils.agent_trace.__main__ import main as trace_main

    trace_main()


def main():
    _configure_console()

    command, command_args = _resolve_command()

    # 注意：通过修改 sys.argv 将子命令参数传递给子模块。
    # 这是 Python CLI 分发子命令的常见模式（pip、setuptools 等也这么用）。
    # 子模块的入口函数依赖 sys.argv 解析参数，此处不改为显式传参以保持兼容。

    if command == "cli":
        sys.argv = [sys.argv[0], *command_args]
        _run_cli(command_args if command_args else None)
        return

    if command in {"web", "server"}:
        sys.argv = [sys.argv[0], *command_args]
        _run_web()
        return

    if command in {"trace", "agent-trace", "agent_trace"}:
        sys.argv = [sys.argv[0], *command_args]
        if len(sys.argv) == 1 and _default_command() == "trace":
            sys.argv.append("serve")
        _run_trace()
        return

    if command in {"-h", "--help", "help"}:
        print(
            "用法:\n"
            "  stock-radar.exe [cli]              进入交互模式\n"
            "  stock-radar.exe cli status --json   一次性命令\n"
            "  stock-radar.exe cli skills list     一次性命令\n"
            "  stock-radar.exe web                 启动 Web 服务\n"
            "  stock-radar.exe trace serve         启动 Trace 服务\n"
        )
        return

    print(f"未知子命令: {command}")
    print("可用子命令: cli, web, trace")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
