"""能力层：调度分配「能力」，不分配「厂商工具」。

为什么需要它（2026-11-01 用户指出）：
`fetch.py` 早期把数据槽直接写成 `mx_data_query → iwc_query`，等于绕过了三层体系——
① skill/tool 编排、② fetcher 的多源降级（`@retry(capability="hist")`，同花顺优先、
max_data 兜底）、③ 用户在「数据源优先级」里的显式顺序。额度一耗尽就直接失效。

本模块只做**声明**（纯数据 + 纯函数，无 IO、无网络）：
- 能力是稳定的语义（序列 / 快照 / 估值 / 财务 / 资金 / 成分 / 一致预期 / 资讯 / 宏观）；
- 每个能力声明两类执行路径：
  * `agent_tools`：交给子代理的**技能工具**（它自己决定调哪个，符合 tool 体系）；
  * `providers`：直接走 fetcher 函数（用于「要全量序列」这种工具会截断的场景），
    由 fetcher 内部完成多源降级与用户优先级。

调用方（调度/取数）只认能力名，不认厂商。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── 能力名（稳定语义，勿改）──
SERIES_HISTORY = "series_history"   # 逐日/逐期序列（收盘、成交额、估值历史）
SNAPSHOT = "snapshot"               # 最新点位/涨跌/成交
VALUATION = "valuation"             # 估值指标与历史分位
FINANCIALS = "financials"           # 财务指标
CAPITAL_FLOW = "capital_flow"       # 个股/板块资金流、北向、两融
CONSTITUENTS = "constituents"       # 板块/指数成分与横截面
CONSENSUS = "consensus"             # 机构评级/一致预期
NEWS = "news"                       # 资讯与事件
MACRO = "macro"                     # 宏观序列
CHIP = "chip"                       # 筹码分布（获利比例/成本区间/集中度）
RISK = "risk"                       # 风险指标（波动/回撤/夏普/Beta）
PEER_FINANCIALS = "peer_financials" # 逐股财务横截面（营收/净利/毛利/净利率/PE/PB，按价格带分组）
PEER_SERIES = "peer_series"         # 多股归一化走势（8–10 条折线，起点=100）

CAPABILITIES: Dict[str, Dict[str, Any]] = {
    SERIES_HISTORY: {
        "name": "历史序列",
        "desc": "某标的在一段区间内的逐日/逐期数值（收盘价、成交额、估值等），用于画走势图",
        "agent_tools": ["get_stock_history", "calc_technical_indicators"],
        "providers": [
            {"kind": "fetcher", "fn": "ak_stock_hist", "capability": "hist",
             "note": "多源降级：hithink → efinance → akshare → baostock → sina → mx_data"},
            {"kind": "fetcher", "fn": "ak_valuation_history", "capability": "valuation",
             "note": "估值历史序列（PB/PE 分位走势）"},
        ],
        "prefer": "full_series",
    },
    SNAPSHOT: {
        "name": "最新快照",
        "desc": "标的最新点位、涨跌幅、成交额、换手等单点数据",
        "agent_tools": ["get_stock_realtime", "get_sector_ranking"],
        "providers": [
            {"kind": "fetcher", "fn": "ak_stock_realtime", "capability": "realtime"},
            {"kind": "fetcher", "fn": "ak_spot_em", "capability": "spot"},
        ],
    },
    VALUATION: {
        "name": "估值与分位",
        "desc": "PB/PE/股息率等估值指标，以及历史分位",
        "agent_tools": ["get_valuation_percentile", "get_valuation_summary",
                        "get_valuation_indicators", "get_industry_valuation_compare",
                        "get_sector_ranking"],
        "notes": "个股用 get_valuation_percentile/get_valuation_indicators；"
                 "指数/板块优先 get_industry_valuation_compare 或 get_sector_ranking"
                 "（个股估值接口对指数会报错）",
        "providers": [
            {"kind": "fetcher", "fn": "ak_valuation_indicators", "capability": "valuation"},
            {"kind": "fetcher", "fn": "ak_valuation_history", "capability": "valuation"},
        ],
    },
    FINANCIALS: {
        "name": "财务",
        "desc": "营收、净利、ROE、不良率、净息差等财务与经营指标",
        "agent_tools": ["get_stock_financial"],
        "providers": [
            {"kind": "fetcher", "fn": "ak_financial_abstract", "capability": "financial"},
        ],
    },
    CAPITAL_FLOW: {
        "name": "资金流",
        "desc": "个股/板块主力净流入、北向资金、两融",
        "agent_tools": ["get_stock_fund_flow", "get_sector_fund_flow", "get_north_fund_flow"],
        "providers": [],
    },
    CONSTITUENTS: {
        "name": "成分与横截面",
        "desc": "板块/指数成分股及其行情、估值横截面",
        "agent_tools": ["get_board_stocks", "get_industry_list", "get_concept_list"],
        "notes": "板块成分股请用**板块名称**调用 get_board_stocks（如「银行」「半导体」），"
                 "不要传指数代码（000134 这类）；指数成分另走 index_constituent",
        "providers": [
            {"kind": "fetcher", "fn": "ak_board_industry_cons", "capability": "board"},
        ],
    },
    CONSENSUS: {
        "name": "机构观点",
        "desc": "机构评级、目标价、一致预期（含卖方研报摘要）",
        "agent_tools": ["get_stock_rating"],
        "notes": "个股评级用 get_stock_rating；板块层观点由研报摘要聚合"
                 "（评级分布 / 目标价中位数 / 近期观点，均带机构与日期）",
        "providers": [
            {"kind": "fetcher", "fn": "fetch_research_consensus", "capability": "consensus",
             "note": "研报摘要聚合（tools/fetcher/consensus_src.py）"},
            {"kind": "fetcher", "fn": "ak_stock_rating", "capability": "rating"},
        ],
    },
    NEWS: {
        "name": "资讯",
        "desc": "个股/板块新闻与事件",
        "agent_tools": ["search_stock_news"],
        "providers": [],
    },
    MACRO: {
        "name": "宏观",
        "desc": "利率、CPI、社零、社融等宏观时间序列",
        "agent_tools": ["get_macro_series"],
        "notes": "消费/白酒：cn_retail_yoy（社零同比）、cpi_yoy、cn10_y（10Y 国债，算股息率利差）；"
                 "周期：au_sge（上海金）、cu_main（沪铜）、rb_main（螺纹钢）；"
                 "猪周期：hog_spot_price/hog_spot_index（生猪现货）",
        "providers": [],
    },
    CHIP: {
        "name": "筹码",
        "desc": "获利比例、平均成本、90% 成本区间与集中度",
        "agent_tools": ["get_chip_distribution"],
        "providers": [],
    },
    RISK: {
        "name": "风险指标",
        "desc": "年化波动、最大回撤、夏普/Sortino、Beta、VaR",
        "agent_tools": ["get_risk_metrics", "calc_technical_indicators"],
        "providers": [],
    },
    PEER_FINANCIALS: {
        "name": "逐股财务横截面",
        "desc": "按模板声明的价格带分组，逐股取营收/净利同比/毛利率/净利率/PE/PB，并给分组中位数",
        "agent_tools": [],          # 由程序直接取数（逐股循环），不交给子代理
        "providers": [
            {"kind": "fetcher", "fn": "ak_financial_abstract", "capability": "financial"},
            {"kind": "fetcher", "fn": "ak_valuation_indicators", "capability": "valuation"},
        ],
        "notes": "模板需提供 stock_tiers（价格带 → 股票列表）；数据由 derive 层逐股取",
    },
    PEER_SERIES: {
        "name": "多股相对走势",
        "desc": "同起点归一化（=100）的个股走势，一张图 8–10 条折线，比较相对强弱",
        "agent_tools": [],
        "providers": [{"kind": "fetcher", "fn": "ak_stock_hist", "capability": "hist"}],
        "notes": "模板需提供 stock_tiers；数据由 derive 层并行取日线",
    },
}

# 旧「数据槽」→ 能力（Step 2 迁移期用，保证老模板不改也能跑）
SLOT_TO_CAPABILITY: Dict[str, str] = {
    "snapshot": SNAPSHOT,
    "valuation": VALUATION,
    "capital_flow": CAPITAL_FLOW,
    "constituents": CONSTITUENTS,
    "consensus_poll": CONSENSUS,
    "industry_space": CONSENSUS,     # 行业空间目前只能靠研报摘要
    "share_cr": CONSENSUS,
    "series_history": SERIES_HISTORY,
    "financials": FINANCIALS,
    "macro": MACRO,
    "news": NEWS,
    "chip": CHIP,
    "risk": RISK,
    "peer_financials": PEER_FINANCIALS,
    "peer_series": PEER_SERIES,
    "verdict": "",                   # 派生量，不取数
}


def all_names() -> List[str]:
    return list(CAPABILITIES)


def get(name: str) -> Dict[str, Any]:
    key = (name or "").strip()
    if key not in CAPABILITIES:
        raise KeyError(f"未知能力: {name!r}（可选：{all_names()}）")
    return CAPABILITIES[key]


def from_slot(slot: str) -> Optional[str]:
    """旧数据槽 → 能力名；无对应（如 verdict）返回 None。"""
    cap = SLOT_TO_CAPABILITY.get(str(slot or "").strip(), "")
    return cap or None


def agent_tools(names: List[str]) -> List[str]:
    """能力集合 → 子代理可用技能工具（去重保序）。"""
    out: List[str] = []
    for n in names:
        for t in get(n).get("agent_tools") or []:
            if t not in out:
                out.append(t)
    return out


def providers(names: List[str], kind: str = "fetcher") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in names:
        for p in get(n).get("providers") or []:
            if p.get("kind") == kind:
                out.append({**p, "capability_name": n})
    return out


def describe_for_prompt(names: Optional[List[str]] = None) -> str:
    """给调度/子代理看的能力目录（只写语义与产出，不写厂商）。"""
    keys = names or all_names()
    lines = []
    for n in keys:
        c = get(n)
        lines.append(f"- {n}（{c['name']}）：{c['desc']}")
    return "\n".join(lines)
