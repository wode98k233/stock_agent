### 意图匹配映射表（Markdown 格式）

| 用户问法示例 | template_id | 仪表盘类别 | scenario_tag | 扩展字段 |
| --- | --- | --- | --- | --- |
| **个股类** |  |  |  |  |
| 给我深入分析贵州茅台 | stock_deep_dive | stock | 个股决策仪表盘 | price_levels, split_advice, position_guidance, falsification_signal |
| 这只股票怎么样 | stock_deep_dive | stock | 个股决策仪表盘 | 同上 |
| 能买吗 | stock_deep_dive | stock | 个股决策仪表盘 | 同上 |
| **市场类** |  |  |  |  |
| 给我生成今天的股市总结 | market_daily | market | 市场决策仪表盘 | index_data, market_breadth, sector_rotation, strong/weak_sectors, volume_summary... |
| 给我生成今天的经济日报 | macro_daily | market | 市场决策仪表盘 | 同上 |
| **板块类** |  |  |  |  |
| 半导体板块怎么样 | sector_timing | sector | 板块决策仪表盘 | market_temperature, sector_stage, leading_stocks, portfolio_action, action_items |
| 给我分析下红利板块，现在是否值得加仓 | sector_timing | sector | 板块决策仪表盘 | 同上 |
| 机器人题材炒作到什么阶段了 | theme_trading | sector | 板块决策仪表盘 | 同上 |
| 分析一下固态电池产业链 | industry_chain | sector | 板块决策仪表盘 | 同上 |
| 恒生互联网ETF今天为什么跳水 | etf_analysis | sector | 板块决策仪表盘 | 同上 |
| 行业轮动全景怎么看 | sector_landscape | sector | 板块决策仪表盘 | 同上 |
| **筛选类** |  |  |  |  |
| 红利股里面有什么值得买的个股吗 | dividend_screening | screening | 筛选决策仪表盘 | candidate_stocks, portfolio_action, action_items, falsification_signal |
| 用多因子帮我选股 | multi_factor_screening | screening | 筛选决策仪表盘 | 同上 |
| 机构怎么看宁德时代目标价 | consensus_view | screening | 筛选决策仪表盘 | 同上 |
| **其他** |  |  |  |  |
| 分析一下贵州茅台最新财报业绩 | earnings_analysis | stock | 个股决策仪表盘 | price_levels, split_advice... |
| 我买了白酒消费，已经连跌好几年了 | portfolio_advice | stock | 个股决策仪表盘 | 同上 |
| 最近美伊冲突对黄金石油影响大吗 | event_impact | market | 市场决策仪表盘 | event_impact, sector_rotation, strong/weak_sectors, falsification_signal |
| 茅台现在估值分位低不低 | valuation_percentile | stock | 个股决策仪表盘 | 同上 |
| 这只新股发行价合理吗 | ipo_analysis | stock | 个股决策仪表盘 | 同上 |
| 趋势跟踪信号如何 | trend_following | stock | 个股决策仪表盘 | 同上 |
| 今天有哪些放量突破的股票 | volume_price_alert | stock | 个股决策仪表盘 | 同上 |

---

**关键修复说明**：
"红利板块" 相关查询之前错误匹配到 `dividend_screening`（匹配得分22分），现已修复为正确匹配 `sector_timing`（匹配得分57分）。
