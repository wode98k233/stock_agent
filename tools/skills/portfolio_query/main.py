"""
选股雷达 - 持仓查询技能
提供用户真实持仓快照、单持仓详情、组合盈亏汇总、交易流水查询能力
（docs/eastmoney持仓同步-设计方案.md §3 链路 3：skill 取代关键词门控为主通道）
"""
from tools.skill_builder import SkillBuilder, skill_tool


class PortfolioQuerySkill(SkillBuilder):
    """持仓查询技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        self._init_core_functions()

    def _init_core_functions(self):
        """初始化核心函数（延迟导入）"""
        from tools.portfolio_data import (
            get_positions as _get_positions,
            get_position_detail as _get_position_detail,
            get_pnl_summary as _get_pnl_summary,
            get_trade_history as _get_trade_history,
        )
        self._get_positions = _get_positions
        self._get_position_detail = _get_position_detail
        self._get_pnl_summary = _get_pnl_summary
        self._get_trade_history = _get_trade_history

    @skill_tool
    def get_positions(self, source: str = "") -> dict:
        """获取用户全部持仓快照。source 可选过滤（eastmoney/manual/signal_eval/backtest），默认全部。
        返回 positions[{code,name,qty,avg_cost,source,is_etf,...}] + account{total_asset,avail_cash} + meta{synced_at,source_counts}；
        东财未同步时 account 为 null 并在 meta 提示。"""
        return self._get_positions(source, self.logger)

    @skill_tool
    def get_position_detail(self, code: str) -> dict:
        """单只持仓详情：数量/成本/市值/浮盈 + 可用数量（卖出限制）+ ETF 标记。
        code 为裸 6 位代码，如"002714"、"513330"。未持仓返回 {held: false}。"""
        return self._get_position_detail(code, self.logger)

    @skill_tool
    def get_pnl_summary(self) -> dict:
        """持仓组合汇总：总市值/总成本/总浮盈/ETF 占比/盈亏 TOP3/来源计数。
        浮盈明细以东财 App 口径（Ckyk/Ljyk）为准。"""
        return self._get_pnl_summary(self.logger)

    @skill_tool
    def get_trade_history(self, code: str = "", limit: int = 50) -> dict:
        """交易流水（手动记账/信号记入部分）。code 可选过滤，limit 默认 50。
        东财同步前的历史为空——返回 meta.note 诚实标注，勿编造历史交易。"""
        return self._get_trade_history(code, limit, self.logger)


def build_tools(logger, memory_mgr):
    """构建工具列表（兼容接口，SkillRegister 入口）"""
    skill = PortfolioQuerySkill(logger, memory_mgr)
    return skill.build_langchain_tools()
