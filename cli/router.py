"""
CLI 命令路由
🔀 核心路由器，处理所有用户输入
"""
import asyncio
import sys
from dataclasses import dataclass


@dataclass
class Result:
    """🔖 统一返回结果"""
    exit: bool = False
    message: str = ""
    response: str = ""
    uuid: str = ""


class Router:
    """
    🔀 命令路由器
    📋 命令映射表 + 处理函数

    执行优先级：
    1. 系统命令（help/debug/info/mode/exit）
    2. 场景模板（条件选股/个股分析/板块分析/市场概览/数据查询/对比分析）
    3. ReAct / Plan & Solve Agent（兜底）
    """

    def __init__(self):
        self._agent_mode = "react_stock"
        self._system_commands = {
            "exit": self._cmd_exit,
            "exit()": self._cmd_exit,
            "quit": self._cmd_exit,
            "debug": self._cmd_debug,
            "info": self._cmd_info,
            "help": self._cmd_help,
            "?": self._cmd_help,
            "h": self._cmd_help,
            "mode": self._cmd_mode,
            "model": self._cmd_mode,
        }
        self._agent_prefix = "mode "

    async def handle(self, user_input: str, context: dict) -> Result:
        """处理用户输入"""
        cmd = user_input.strip().lower()

        handler = self._system_commands.get(cmd)
        if handler:
            return await handler(user_input, context)

        if cmd.startswith(self._agent_prefix):
            return await self._cmd_mode_change(user_input, context)

        return await self._execute_agent(user_input, context)

    async def _cmd_exit(self, user_input: str, context: dict) -> Result:
        from cli.banner import build_exit_stats
        session_stats = context.get("session_stats")
        msg = build_exit_stats(session_stats)
        return Result(exit=True, message=msg)

    async def _cmd_debug(self, user_input: str, context: dict) -> Result:
        from config import Config
        from utils.logger import set_global_log_level
        Config.set_log_level('DEBUG')
        set_global_log_level('DEBUG')
        return Result(message="[OK] 🔧 DEBUG mode")

    async def _cmd_info(self, user_input: str, context: dict) -> Result:
        from config import Config
        from utils.logger import set_global_log_level
        Config.set_log_level('INFO')
        set_global_log_level('INFO')
        return Result(message="[OK] ℹ️ INFO mode")

    async def _cmd_help(self, user_input: str, context: dict) -> Result:
        msg = """
📖 可用命令:
  help, h, ?     - 显示帮助
  mode [name]    - 切换 Agent 模式
  debug          - 开启调试模式
  info           - 开启信息模式
  exit, quit    - 退出

📦 Agent 模式:
  react_stock    - ReAct 动态规划（自驱式，适合简单查询）
  plan_solve     - Plan & Solve（先计划后执行，适合复杂分析）
  unified_plan   - 统一执行（一个 Agent 按计划执行所有步骤）
  scenario       - 场景快速路径（正则分类，低延迟）

💡 或直接输入股票分析问题
        """.strip()
        return Result(message=msg)

    async def _cmd_mode_change(self, user_input: str, context: dict) -> Result:
        parts = user_input.strip().split()
        if len(parts) < 2:
            return await self._cmd_mode(user_input, context)

        mode_name = parts[1]
        from agents.factory import AgentFactory
        try:
            AgentFactory.set_default(mode_name)
            self._agent_mode = mode_name
            return Result(message=f"[OK] 🔄 Switched to {mode_name}")
        except ValueError as e:
            return Result(message=f"[ERROR] {e}")

    async def _cmd_mode(self, user_input: str, context: dict) -> Result:
        from agents.factory import AgentFactory
        agents = AgentFactory.list()
        msg = f"📋 当前模式: {self._agent_mode}\n📦 可用模式: {agents}"
        return Result(message=msg)

    async def _execute_agent(self, user_input: str, context: dict) -> Result:
        """🚀 执行 Agent"""
        from utils.logger import get_logger

        logger, uuid, ctx = get_logger(user_input)

        from agents.factory import AgentFactory
        from tools.skills import SkillRegistry
        from utils.progress import default_progress_callback

        agent = AgentFactory.get(self._agent_mode)
        agent.on_startup()

        memory = context["memory"]
        register = context["skill_register"]
        registry = SkillRegistry(logger, memory, register)

        def progress_callback(event):
            default_progress_callback(event)

        response = await agent.run(user_input, registry, memory, logger, progress_callback=progress_callback)

        # 场景Agent返回空字符串时，降级到默认Agent
        if not response and self._agent_mode == "scenario":
            logger.info("场景Agent返回空，降级到 react_stock")
            fallback = AgentFactory.get("react_stock")
            fallback.on_startup()
            response = await fallback.run(user_input, registry, memory, logger, progress_callback=progress_callback)

        session_stats = context.get("session_stats")
        if session_stats and ctx:
            session_stats.accumulate(ctx)

        return Result(response=response, uuid=uuid)

    def output(self, result: Result):
        """📺 输出结果"""
        if result.exit:
            print(result.message)
            return True

        if result.message:
            print(result.message)
            return False

        if result.response:
            print("\n" + "=" * 60)
            print("📊 分析结果")
            print("-" * 60)
            print(result.response)
            print("=" * 60)
            if result.uuid:
                print(f"📝 日志UUID: {result.uuid} | 日志文件: logs/{result.uuid}.log")

        return False


router = Router()
