# 测试指南

## 三层测试架构

```
test/
├── unit/          # 单元测试 — 不联网，不调 LLM（300+ tests）
├── integration/   # 集成测试 — 联网调数据源 API，不调 LLM（42 tests）
└── e2e/           # 端到端  — 联网 + 调 LLM，测完整 Agent 流程（11 tests）
```

## 常用命令

```bash
# 只跑单元测试（最快，不需要网络和 API key）
pytest test/unit/ -v

# 只跑集成测试（需要网络，不需要 API key）
pytest test/integration/ -v -m integration

# 只跑 e2e 测试（需要网络 + OPENAI_API_KEY）
pytest test/e2e/ -v -m e2e

# 跑全部测试
pytest test/ -v

# 跑单个文件
pytest test/unit/test_budget.py -v

# 跑单个用例
pytest test/unit/test_budget.py::test_budget_token_limit -v

# 按关键词匹配
pytest test/unit/ -v -k "cache"

# 只跑慢测试
pytest test/ -v -m slow

# 停在第一个失败
pytest test/unit/ -x

# 显示详细失败信息
pytest test/unit/ -v --tb=short
```

## 改了什么 → 跑什么

| 你改的模块 | 跑哪些测试 | 命令 |
|------------|-----------|------|
| `utils/budget.py` | unit/test_budget.py | `pytest test/unit/test_budget.py -v` |
| `utils/cache.py` | unit/test_cache.py | `pytest test/unit/test_cache.py -v` |
| `utils/logger.py` | unit/test_logger.py, test_llm_callbacks.py, test_plan_steps/test_logger_skip_db.py | `pytest test/unit/test_logger.py test/unit/test_llm_callbacks.py test/unit/test_plan_steps/test_logger_skip_db.py -v` |
| `utils/intent.py` | unit/test_intent.py | `pytest test/unit/test_intent.py -v` |
| `utils/llm_factory.py` | unit/test_llm_callbacks.py, test_retry_context.py | `pytest test/unit/test_llm_callbacks.py test/unit/test_retry_context.py -v` |
| `utils/agent_trace/` | unit/test_agent_trace.py, test_agent_trace_chain.py | `pytest test/unit/test_agent_trace.py test/unit/test_agent_trace_chain.py -v` |
| `utils/session_stats.py` | unit/test_session_stats.py | `pytest test/unit/test_session_stats.py -v` |
| `agents/` (Agent 逻辑) | unit + e2e | `pytest test/unit/test_agent_utils.py test/e2e/ -v` |
| `agents/scenarios/` | unit/test_scenario_router.py, test_scenario_common.py | `pytest test/unit/test_scenario_router.py test/unit/test_scenario_common.py -v` |
| `agents/plan/` | unit/test_plan_package.py, unit/test_plan_steps/ (结构), e2e/test_plan_steps/ (真实 LLM) | `pytest test/unit/test_plan_package.py test/unit/test_plan_steps/ -v` → 然后 `pytest test/e2e/test_plan_steps/ -v -m e2e` |
| `tools/skills.py` | unit/test_skill_register.py, test_new_skills.py | `pytest test/unit/test_skill_register.py test/unit/test_new_skills.py -v` |
| `tools/aggregator.py` | unit/test_tools.py | `pytest test/unit/test_tools.py -v` |
| `tools/fetcher/` | integration/test_fetcher.py | `pytest test/integration/test_fetcher.py -v` |
| `tools/other_skills/eastmoney/` | integration/test_mx_data_skills.py, unit/test_mx_data_ds.py | `pytest test/unit/test_mx_data_ds.py test/integration/test_mx_data_skills.py -v` |
| 新增/修改 skill | unit/test_new_skills.py + integration/test_new_skills_integration.py | `pytest test/unit/test_new_skills.py test/integration/test_new_skills_integration.py -v` |
| `tools/valuation.py` | unit/test_valuation.py | `pytest test/unit/test_valuation.py -v` |
| `config.py` | unit/test_config_validate.py | `pytest test/unit/test_config_validate.py -v` |
| LLM prompt / Agent 全流程 | e2e | `pytest test/e2e/ -v -m e2e` |

**不确定跑什么？** 先跑 `pytest test/unit/ -v`，过了再跑对应的 integration 或 e2e。
