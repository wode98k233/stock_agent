TRACE_ICONS = {
    "llm": "🤖", "tool": "🔧", "chain": "🔗", "retriever": "📄",
    "agent": "🤖", "tools": "🔧", "planner": "📋", "executor": "⚡",
    "replanner": "🔄", "classifier": "🏷️", "graph": "🌳",
    "classify": "🏷️", "select_template": "📄", "select_skills": "🎯",
    "prepare": "🧩", "template_report": "📊", "dashboard": "📈",
    "partial_summary": "📝", "unified_executor": "🚀",
}

# 溯源面板：工具名 → 领域/提供方/展示名/图标
# 用于「回答溯源 · 执行归因」面板把 step_name 归类为
# 行情 / 基本面 / 资金 / 资讯 / 推理（ADR-002 / system-design.md §4）。
# 未命中 key → 归为「其他」（由 storage._aggregate_attribution 处理）。
# 注意：key 必须与工具注册表（tools/skills.py / tools/other_skills/*）的实际工具名一致，
# 历史坑：曾用占位名（fetch_market_data 等）与实际工具名（mx_data_query 等）脱节，
# 导致真实数据源全部落入「其他」、sources 只剩「推理 · LLM」。
TOOL_DOMAIN_MAP = {
    # ── 实际工具（tools/other_skills/ 注册名） ──
    # 数据（综合金融数据查询：行情/财务/关联关系）
    "mx_data_query": {"domain": "数据", "provider": "妙想", "display_name": "妙想数据查询", "icon": "doc"},
    "mx_xuangu_filter": {"domain": "数据", "provider": "妙想", "display_name": "妙想智能选股", "icon": "doc"},
    "iwc_market_query": {"domain": "数据", "provider": "问财", "display_name": "问财市场查询", "icon": "doc"},
    "iwc_sector_selector": {"domain": "数据", "provider": "问财", "display_name": "问财板块选择", "icon": "doc"},
    # 资讯
    "mx_search_news": {"domain": "资讯", "provider": "妙想", "display_name": "妙想新闻检索", "icon": "news"},
    "iwc_announcement_search": {"domain": "资讯", "provider": "问财", "display_name": "问财公告搜索", "icon": "news"},
    # 推理（分析引擎，非数据源）
    "llm_tech_interpret": {"domain": "推理", "provider": "LLM", "display_name": "LLM 技术解读", "icon": "brain"},
    "llm_build_report": {"domain": "推理", "provider": "LLM", "display_name": "LLM 报告生成", "icon": "brain"},
    # ── 兼容旧映射（如自定义工具沿用旧名） ──
    # 行情
    "fetch_market_data": {"domain": "行情", "provider": "AKShare", "display_name": "历史行情", "icon": "chart"},
    "get_quote": {"domain": "行情", "provider": "AKShare", "display_name": "实时报价", "icon": "chart"},
    "get_history": {"domain": "行情", "provider": "AKShare", "display_name": "历史行情", "icon": "chart"},
    # 基本面
    "fetch_fundamental": {"domain": "基本面", "provider": "财务库", "display_name": "基本面数据", "icon": "doc"},
    "get_fundamental": {"domain": "基本面", "provider": "财务库", "display_name": "基本面数据", "icon": "doc"},
    # 资金
    "fetch_northbound": {"domain": "资金", "provider": "北向资金", "display_name": "北向净流入", "icon": "fund"},
    "get_northbound": {"domain": "资金", "provider": "北向资金", "display_name": "北向净流入", "icon": "fund"},
    "fetch_fund_flow": {"domain": "资金", "provider": "资金流", "display_name": "主力资金流", "icon": "fund"},
    # 资讯
    "search_news": {"domain": "资讯", "provider": "搜索", "display_name": "新闻检索", "icon": "news"},
    "fetch_news": {"domain": "资讯", "provider": "资讯源", "display_name": "资讯聚合", "icon": "news"},
    # 推理
    "llm_reason": {"domain": "推理", "provider": "LLM", "display_name": "推理分析", "icon": "brain"},
    "agent": {"domain": "推理", "provider": "LLM", "display_name": "Agent 决策", "icon": "brain"},
}
