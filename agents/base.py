"""
🤖 Agent 基类
所有 Agent 模式需继承此基类
"""
import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime

from utils.logger import ensure_radar
from utils.memory import iter_history_messages
from memory.sdk import get_sdk
from memory.user_profile import UserProfileManager


def _build_memory_context(user_input: str, history: list = None) -> Optional[str]:
    """从 session 历史提取最近的用户提问，拼接成记忆检索上下文。

    仅取历史中的 human/user 消息（排除当前这一条，避免与 query 重复），
    最多取最近 4 条。无历史时返回 None（退化成仅用当前 query 检索）。
    """
    if not history:
        return None
    human_texts = []
    cur = (user_input or "").strip()
    for role, text in iter_history_messages(history):
        if role == "user" and text != cur:
            human_texts.append(text)
    if not human_texts:
        return None
    return "\n".join(human_texts[-4:])


# 持仓账本注入门控关键词（portfolio_advice 路由关键词的核心子集，见 route_rules.json）
_PORTFOLIO_KEYWORDS = (
    "持仓", "清仓", "减仓", "补仓", "加仓", "建仓", "割肉", "止损", "止盈",
    "仓位", "浮亏", "浮盈", "回本", "套牢", "解套", "满仓", "我买", "我卖",
)


def _build_portfolio_block(user_input: str, db_path: str | None = None) -> str:
    """命中持仓关键词且有持仓时，构造 [用户持仓账本] 注入块。

    三层防护：关键词门控（无关问题零成本短路）→ 开关（PORTFOLIO_ENRICH_ENABLED）
    → 异常全吞（表不存在/DB 损坏返回空串，不影响主流程）。
    直接 sqlite 只读 stock_radar.db 的 portfolio_position 快照表——
    agents 层不反向依赖 server 包。
    """
    if not any(kw in (user_input or "") for kw in _PORTFOLIO_KEYWORDS):
        return ""
    try:
        from config import Config
        if not Config.PORTFOLIO_ENRICH_ENABLED:
            return ""
        import sqlite3
        from utils.app_paths import get_db_path
        conn = sqlite3.connect(db_path or get_db_path())
        try:
            # 兜底注入（主通道=portfolio_query 技能）：带 ETF 标记与来源
            rows = conn.execute(
                "SELECT stock_code, stock_name, qty, avg_cost, etf_flag, source"
                " FROM portfolio_position WHERE qty > 0"
                " ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        # 账户资金/组合当日盈亏（东财同步后有值；无则跳过，不编造）
        account_line = ""
        try:
            from server.services.eastmoney_sync import get_account_cache
            acct = get_account_cache(db_path=db_path) or {}
            acct_parts = []
            if acct.get("total_asset") is not None:
                acct_parts.append(f"账户总资产约{acct['total_asset']:,.2f}元")
            if acct.get("avail_cash") is not None:
                acct_parts.append(f"可用资金约{acct['avail_cash']:,.2f}元")
            if acct.get("today_pnl") is not None:
                acct_parts.append(f"今日盈亏{acct['today_pnl']:+,.2f}元")
            if acct.get("snapshot_at") and acct.get("market_status"):
                status = acct["market_status"]
                note = "，最新价为盘中快照会波动" if status == "盘中" else ""
                acct_parts.append(
                    f"同步于{acct['snapshot_at']}（{status}{note}）")
            if acct_parts:
                account_line = "账户：" + " · ".join(acct_parts)
        except Exception:
            account_line = ""
        total_cost = sum(cost * qty for _, _, qty, cost, _, _ in rows)
        etf_cost = sum(cost * qty for _, _, qty, cost, flag, _ in rows if flag)
        etf_pct = etf_cost / total_cost * 100 if total_cost else None
        lines = []
        if account_line:
            lines.append(account_line)
        lines.append("[用户持仓账本]（兜底注入；可用 portfolio_query 技能查询明细）")
        if etf_pct is not None:
            lines.append(f"ETF 占组合成本约 {etf_pct:.1f}%")
        for code, name, qty, cost, etf_flag, source in rows:
            src = "东财" if (source or "manual") == "eastmoney" else "手动"
            etf = " ETF" if etf_flag else ""
            lines.append(
                f"{name or code}({code}) 成本{cost:.2f}×{qty:g}股{etf}〔{src}〕")
        return "\n".join(lines)
    except Exception:
        return ""


class BaseAgent:
    """
    🤖 Agent 基类
    📋 定义 Agent 的标准接口
    """

    name: str = ""
    description: str = ""

    async def run(self, user_input: str, registry, memory, logger, progress_callback: Optional[Callable] = None):
        """
        🚀 执行 Agent
        📥 user_input: 用户输入
        📥 registry: Skill 注册器
        📥 memory: 记忆管理器
        📥 logger: 日志器
        📥 progress_callback: 进度回调函数
        📤 str: Agent 执行结果
        """
        raise NotImplementedError

    def on_startup(self):
        """
        🆕 Agent 启动时的初始化：清理过期缓存
        """
        from utils.cache import async_clean_expired_cache
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_clean_expired_cache())
        except RuntimeError:
            pass

    def warmup_graph(self, logger=None):
        """预热 Graph 编译（后台线程调用）。

        子类可覆写以预编译 Graph，让首次请求时 compile 几乎瞬间完成。
        基类默认不做任何事。
        """
        pass

    def _update_intent_memory(self, user_input: str, final_response: str, logger):
        """更新用户画像（非阻塞）。

        实体提取策略:
          1. 从 user_input 提取板块/个股 (短文本, 准确)
          2. 从 ContextVar 工具提取补充股票代码 (工具返回的具体股票)
          3. 不再正则解析完整报告文本 → 避免垃圾stock名
        """

        logger = ensure_radar(logger)
        try:
            profile = UserProfileManager(user_id="default")
            result_str = final_response if isinstance(final_response, str) else str(final_response or "")

            # 1. 从用户输入的短文本提取板块/个股 (比正则解析报告全文准确得多)
            sectors, stocks = profile.extract_entities(user_input)

            # 2. 从 ContextVar 工具提取中补充股票代码和名称
            try:
                from memory.metadata import _tool_extracts, _INIT_SENTINEL
                extracts = _tool_extracts.get()
                if extracts is not _INIT_SENTINEL:
                    for ex in extracts:
                        code = ex.get("stock_code")
                        name = ex.get("stock_name")
                        if code and code not in stocks:
                            stocks.append(code)
                        if name and name not in stocks and len(name) >= 2:
                            stocks.append(name)
            except Exception as e:
                logger.error(f"从 ContextVar 工具提取中补充股票代码和名称失败: {e}")

            # 3. 从对话隐式提取风险偏好/投资周期等
            profile.extract_from_conversation(user_input)

            profile.update(
                query=user_input,
                result=result_str,
                sectors=sectors,
                stocks=stocks,
            )
        except Exception as e:
            logger.error("I", f"[UserProfile] 更新失败（非阻塞）: {e}")

    def _enrich_user_input(self, user_input: str, progress_callback=None,
                           history: list = None, logger=None) -> str:
        """构建增强输入。enrich_with_time 已生成 [背景信息]+[用户问题] 骨架，
        我们只追加画像和记忆，不重复加 [用户问题] 头。

        同时把「用户原始输入 / 用户画像 / 用户相关记忆片段」三块通过
        progress_callback 推给前端，让页面也能展示（不再只是 agent 内部）。

        Args:
            history: session 历史消息（LangChain 消息元组列表），用于拼接记忆检索上下文。
            logger: 请求级 logger（RadarLogger）；传入后记忆检索统计落进本次请求日志。
        """
        from output.time_util import enrich_with_time
        enriched = enrich_with_time(user_input)
        profile_ctx = ""
        memory_block = ""
        market_status = ""

        # 市场开市/休市状态
        try:
            from utils.trading_calendar import get_market_status_text
            market_status = get_market_status_text()
            if market_status and "正常开市" not in market_status:
                enriched += f"\n[市场状态] {market_status}"
        except Exception:
            pass

        # 用户画像
        try:
            profile = UserProfileManager(user_id="default")
            profile_ctx = profile.get_context_hint() or ""
            if profile_ctx:
                enriched += "\n" + profile_ctx
        except Exception:
            profile_ctx = ""

        # 相关记忆片段（方案A：拼接 session 历史提升召回）
        try:
            sdk = get_sdk()
            context = _build_memory_context(user_input, history)
            memory_block = sdk.retrieve(user_input, logger=logger, context=context) or ""
            if memory_block:
                enriched += "\n" + memory_block
        except Exception:
            memory_block = ""

        # 持仓账本（关键词门控 + 无持仓静默跳过 + 异常全吞，见 _build_portfolio_block）
        try:
            portfolio_block = _build_portfolio_block(user_input)
            if portfolio_block:
                enriched += "\n" + portfolio_block
        except Exception:
            portfolio_block = ""

        # ── A8 sr_* 上下文注入（agents 侧单点）：场景 + 注入摘要哈希 ──
        # 保守原则：任一键取不到就不注入；异常全吞，绝不影响增强输入主流程。
        # 只哈希个性化/状态类注入块（市场状态/画像/记忆/持仓），不含时间前缀
        # （时间逐分钟变化，入哈希会让缓存键必 miss）；无注入 → 固定哨兵 NOINJ。
        try:
            from agents.sr_context import (
                compute_inject_hash,
                note_run_inject_hash,
                note_run_scenario,
            )
            from agents.scenario_router import classify_scenario

            scenario, _ = classify_scenario(user_input)
            note_run_scenario(scenario)
            inject_blocks = [b for b in (market_status, profile_ctx, memory_block, portfolio_block) if b]
            note_run_inject_hash(
                compute_inject_hash(*inject_blocks) if inject_blocks else "NOINJ"
            )
        except Exception:
            pass

        # 推送「记忆上下文」进度事件：用户原始输入 / 用户画像 / 用户相关记忆片段
        # 始终推送：即便画像/记忆为空，用户也能在页面看到「用户原始输入」这一块，
        # 避免空记忆场景下页面什么都不显示（与用户预期「页面应能展示记忆上下文」一致）。
        if progress_callback:
            try:
                from utils.progress import ProgressEvent, ProgressType
                progress_callback(ProgressEvent(
                    type=ProgressType.MEMORY_CONTEXT,
                    message="🧠 记忆上下文已构建",
                    data={
                        "raw_input": user_input,
                        "profile": profile_ctx,
                        "memory_fragments": memory_block,
                    },
                ))
            except Exception:
                pass

        return enriched
