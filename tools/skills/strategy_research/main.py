"""选股雷达 - 策略研发技能（A3）

把自然语言交易想法变成经 OOS 验证的策略研究任务：
- create_strategy_research: 创建后台研究任务（LLM 生成 → 编译 → IS/OOS 回测 → 迭代）
- get_strategy_research_status: 查询任务进度与最优策略卡
- adopt_best_strategy: 最优策略落地（save / monitor / track）

实现上直接调用 server.routes.strategy_research.start_strategy_run 同一入口
（同进程内函数调用，不走 HTTP 自调用），保证面板与对话两个入口行为一致。
"""
import re
from typing import List

from tools.skill_builder import SkillBuilder, skill_tool

_CODE_RE = re.compile(r'^(sh|sz|bj)?[0-9]{6}$')


def _is_tradable_code(code: str) -> bool:
    """标的代码是否可回测（6 位数字，或 sh/sz/bj 前缀）。"""
    return bool(_CODE_RE.match((code or '').strip().lower()))


class StrategyResearchSkill(SkillBuilder):
    """策略研发技能"""

    @skill_tool
    def create_strategy_research(self, idea: str, universe: str = "",
                                 days: int = 1095,
                                 entry_timing: str = "next_open",
                                 exit_timing: str = "") -> dict:
        """创建策略研究任务。输入自然语言交易想法和 6 位 A 股代码，后台自动执行
        LLM 生成策略 → 编译校验 → IS/OOS 双段回测 → 最多 8 轮迭代选优。

        槽位闸门（兜底层）：universe 为空或不是可回测代码、idea 为空时，
        返回 {code: 1, need_clarify: True, missing: [...]}，必须先向用户问清，
        不得用默认标的代替。

        成交口径（买卖各自独立，三档枚举）：
        - entry_timing：next_open（次日开盘，默认）/ same_close（当日收盘价，
          表达「尾盘买入」）/ next_close（次日收盘）
        - exit_timing：留空（默认）= 跟随 entry_timing；也可显式给上面三档之一，
          用来表达「尾盘买入、次日开盘卖出」这类买卖不同口径的需求。"""
        from datetime import datetime, timedelta
        from server.routes.strategy_research import start_strategy_run

        idea = (idea or '').strip()
        universe = (universe or '').strip().lower()
        missing: List[str] = []
        if not idea:
            missing.append('idea')
        if not universe:
            missing.append('universe')
        elif not _is_tradable_code(universe):
            return {
                'code': 1, 'need_clarify': True, 'missing': ['universe'],
                'msg': '标的 ' + universe + ' 不是可回测的代码（6 位数字，或 sh/sz/bj 前缀）。请先确认标的。',
            }
        if universe[:2] in ('sh', 'sz', 'bj'):
            # 归一到纯 6 位数字：下游 start_strategy_run 只接受 ^\d{6}$
            # （server/routes/strategy_research.py:126）
            universe = universe[2:]
        if missing:
            hints = {'idea': '想验证的买点/卖点条件', 'universe': '标的（哪只股票/ETF/指数）'}
            return {
                'code': 1, 'need_clarify': True, 'missing': missing,
                'msg': '还缺：' + '、'.join(hints[m] for m in missing) + '。先问清楚再跑，不要用默认标的代替。',
            }
        days = max(180, min(int(days or 1095), 3650))
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        return start_strategy_run(idea, universe, period_start=start,
                                  period_end=end, source='skill',
                                  entry_timing=entry_timing,
                                  exit_timing=exit_timing)

    @skill_tool
    def get_strategy_research_status(self, run_uuid: str) -> dict:
        """查询策略研究任务进度。输入任务标识（run_uuid），返回状态、轮次摘要与最优策略卡。"""
        from server.routes.strategy_research import _run_detail_payload
        return _run_detail_payload(run_uuid)

    @skill_tool
    def adopt_best_strategy(self, run_uuid: str, action: str) -> dict:
        """最优策略落地。action 取 save（存策略库）/ monitor（止损盯盘）/ track（信号跟踪）。"""
        from utils.cache.backtest_db import get_strategy_run
        from server.routes.strategy_research import (
            _adopt_save, _adopt_monitor, _adopt_track,
        )

        run = get_strategy_run(run_uuid)
        if not run:
            return {'code': 1, 'msg': f'任务不存在: {run_uuid}'}
        if run.get('status') != 'done' or not run.get('best_iter_id'):
            return {'code': 1, 'msg': '任务未完成或没有最优策略'}
        best = _get_best(run)
        handler = {'save': _adopt_save, 'monitor': _adopt_monitor, 'track': _adopt_track}.get(action)
        if handler is None:
            return {'code': 1, 'msg': f'未知动作: {action}（可选 save/monitor/track）'}
        return handler(run, best)


def _get_best(run: dict):
    """读取任务最优轮次记录。"""
    from utils.cache.backtest_db import get_strategy_iter_by_id
    return get_strategy_iter_by_id(run['best_iter_id'])


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口）"""
    skill = StrategyResearchSkill(logger, memory_mgr)
    return skill.build_langchain_tools()
