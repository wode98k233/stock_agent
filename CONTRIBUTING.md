# 二次开发指南

本文面向想要扩展本项目功能的开发者。即使你只打算自用，了解扩展点也能让你更低成本地把它改造成趁手的工具。

---

## 目录

- [开发环境](#开发环境)
- [架构总览](#架构总览)
- [扩展点一：新增数据源](#扩展点一新增数据源)
- [扩展点二：新增技能](#扩展点二新增技能)
- [扩展点三：新增 Agent 模式](#扩展点三新增-agent-模式)
- [扩展点四：新增报告模板](#扩展点四新增报告模板)
- [扩展点五：新增通知渠道](#扩展点五新增通知渠道)
- [扩展点六：新增回测策略](#扩展点六新增回测策略)
- [配置系统](#配置系统)
- [测试规范](#测试规范)
- [代码规范](#代码规范)

---

## 开发环境

```bash
git clone https://github.com/wode98k233/stock_agent.git
cd stock_agent
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 开发期推荐开启的两个开关
echo "WEB_DEV_RELOAD=true"  >> .env   # 改代码不用重启 Web
echo "LOG_LEVEL=DEBUG"      >> .env   # 完整日志
echo "ENABLE_TRACE=true"    >> .env   # 调用链追踪
```

**调试建议**：遇到分析结果不符合预期，先用 `/trace` 命令回看完整调用链，确认是哪一步的工具返回出了问题，再定位代码。比直接读日志快得多。

---

## 架构总览

> **关于失效文档引用**：源码注释中出现的 `docs/superpowers/specs/*.md` 路径指向开发者的内部设计文档，**不在本公开仓库中**。这些是纯注释，不影响运行。相关模块的实现本身即是文档，可直接阅读源码（如 `utils/llm/gateway.py`、`agents/analysis/chart_extractor.py`）。

```
用户输入
   │
   ▼
┌─────────────────────────────────────────────┐
│  CLI 层  cli/                                │  TUI / REPL / 一次性命令
├─────────────────────────────────────────────┤
│  Agent 层  agents/                           │  6 种执行模式
│  react / plan / pdor / group / scenarios     │  LangGraph 状态图驱动
├─────────────────────────────────────────────┤
│  技能层  tools/skills/                       │  12 个技能，自动发现注册
├─────────────────────────────────────────────┤
│  工具层  tools/                              │  指标计算 / 估值 / 情绪 / 聚合
├─────────────────────────────────────────────┤
│  数据层  tools/fetcher/                      │  11 个数据源，优先级降级
└─────────────────────────────────────────────┘
   │                │                │
   ▼                ▼                ▼
memory/         utils/            backtest/
记忆检索      LLM网关/缓存/预算/日志   回测引擎
   │
   ▼
server/  Web 工作台（FastAPI + 前端）
```

**关键设计**：技能与数据源都是**自动发现**的。新增技能/数据源不需要修改任何注册表文件，只要按约定放置目录/类即可。

---

## 扩展点一：新增数据源

数据源是项目的地基。已有 Sina、AKShare、BaoStock、eFinance、Tushare、PyTdx、腾讯、同花顺官方、yfinance、Finnhub、Longbridge 共 11 个。

### 步骤

**1. 复制模板**

```bash
cp tools/fetcher/datasource_template.py tools/fetcher/mydata_ds.py
```

**2. 继承 `DataSource` 并实现方法**

```python
# tools/fetcher/mydata_ds.py
import pandas as pd
from tools.fetcher import DataSource, DataSourceManager


class MyDataDataSource(DataSource):
    name: str = "mydata"      # 必须全局唯一
    priority: int = 85        # 数字越大越优先，建议避开已占用值
    enabled: bool = True      # 设为 False 可临时停用

    @classmethod
    def is_available(cls) -> bool:
        """可用性检查。没填 Key 就返回 False，框架会自动跳过。"""
        from config import Config
        if not Config.MYDATA_API_KEY:
            cls.enabled = False
            return False
        return cls.enabled

    @classmethod
    def get_stock_hist(cls, symbol: str, period: str = "daily",
                       start: str = "", end: str = "") -> pd.DataFrame:
        """历史 K 线。返回的 DataFrame 必须包含列：
        date / open / high / low / close / volume
        """
        ...

    @classmethod
    def get_spot_em(cls) -> pd.DataFrame:
        """全 A 股实时行情快照"""
        ...
```

**可选实现的方法**（不实现则调用时自动降级到下一个数据源）：

| 方法 | 用途 |
| --- | --- |
| `get_stock_realtime(symbol)` | 单只股票实时行情 |
| `get_board_industry_cons(symbol)` | 行业板块成分股 |
| `get_board_concept_cons(symbol)` | 概念板块成分股 |
| `get_board_industry_list()` | 行业板块列表 |
| `get_board_concept_list()` | 概念板块列表 |
| `get_stock_news(symbol)` | 个股新闻 |
| `get_market_news(query, limit)` | 市场新闻 |
| `get_stock_rating(symbol)` | 机构评级 |
| `get_financial_abstract(symbol)` | 财务摘要 |

**3. 注册到管理器**

在 `tools/fetcher/__init__.py` 的注册段加入一行：

```python
DataSourceManager.register_source(MyDataDataSource)   # 85
```

注意方法名是 `register_source`，不是 `register`。注册按优先级从高到低排列，方便阅读。

**4. 加入配置**（可选，用于支持优先级覆盖）

在 `config.py` 的 `Config` 类中加入 Key，并在 `.env.example` 中补充说明。

**5. 写单元测试**

```python
# test/unit/datasource/test_mydata_ds.py
import pytest

@pytest.mark.unit
def test_is_available_without_key(monkeypatch):
    monkeypatch.delenv("MYDATA_API_KEY", raising=False)
    from tools.fetcher.mydata_ds import MyDataDataSource
    assert MyDataDataSource.is_available() is False
```

> **注意**：数据源方法必须自己处理超时与异常，返回空 `DataFrame` 或抛出异常都可以，框架会自动降级。不要在数据源内部 `sys.exit()`。

---

## 扩展点二：新增技能

技能是 Agent 可调用的能力单元。已有 12 个：`stock_query`、`technical_analysis`、`valuation`、`money_flow`、`sector_rotation`、`block_trades`、`margin_trading`、`risk_metrics`、`sentiment_analysis`、`fund_query`、`aggregation`、`special_data`。

### 目录约定

```
tools/skills/my_skill/
├── main.py       # 实现，必须提供 build_tools(logger, memory_mgr)
└── SKILL.md      # 元数据 + 给 LLM 看的说明书
```

**两个文件缺一不可**，`SKILL.md` 不只是给人看的——它的 front-matter 会被解析，正文会被拼进 LLM 的提示词。

### 步骤

**1. 写 `main.py`**

```python
# tools/skills/my_skill/main.py
from tools.skill_builder import SkillBuilder, skill_tool


class MySkill(SkillBuilder):
    """我的技能"""

    def __init__(self, logger, memory_mgr=None):
        super().__init__(logger, memory_mgr)
        # 依赖建议在 __init__ 里延迟导入，避免启动期加载重依赖
        from tools.stock_data import get_stock_history
        self._get_stock_history = get_stock_history

    @skill_tool
    def do_something(self, symbol: str, days: int = 30) -> dict:
        """一句话说明这个工具干什么。这段 docstring 会直接给 LLM 看，
        写得越具体，LLM 越不容易调错。"""
        try:
            df = self._get_stock_history(symbol, days, self.logger)
        except Exception as e:
            # 返回结构化错误，并明确告知 LLM 该走哪条退路
            return {
                "symbol": symbol,
                "error": str(e),
                "fallback": '改用 mx_data_query 查询，不要反复重试本工具',
                "retry": False,
            }
        return {"symbol": symbol, "rows": len(df)}


def build_tools(logger, memory_mgr):
    """必须提供的入口，框架自动调用"""
    return MySkill(logger, memory_mgr).build_langchain_tools()
```

**2. 写 `SKILL.md`**

```markdown
---
name: my_skill
version: v1.0
description: 一句话描述这个技能做什么
category: 数据分析
---

# Skill名称：我的技能
## 基础信息
- 版本：v1.0
- 描述：...
- 所属分类：数据分析
- 适用场景：...

## 工具列表
### 工具1：do_something
- 功能：...
- 参数约束：
  - symbol: str（必填）- 股票代码，如"600519"
  - days: int（可选）- 天数，默认 30

## 使用指南
### 适用场景
- ...

### 不适用场景
- ...（**这一节很重要**，能显著减少 LLM 误用）

### 典型调用
1. ...

### 失败 fallback
- ...
```

**3. 重启，验证加载**

在 CLI 中执行 `/skills` 查看已加载技能列表，或用代码检查：

```bash
python -c "
from tools.skill_register import SkillRegister
r = SkillRegister()
print([m['skill_name'] for m in r.get_skill_catalog()])
"
```

技能目录会被 `SkillRegister` 自动扫描，**不需要手动注册**。

### 写技能的三个经验

1. **`docstring` 就是提示词**。写给 LLM 看，要写清楚输入格式、边界、返回结构。
2. **一定要写"不适用场景"**。这能减少一半的误调用。
3. **失败要返回 `retry: false` 和明确的退路**，否则 LLM 会反复重试同一个失败的工具，把预算烧光。

---

## 扩展点三：新增 Agent 模式

已有 6 种模式，注册名：`react_stock`、`plan_solve`、`unified_plan`、`pdor`、`scenario`、`agent_group`。

### 步骤

**1. 继承 `BaseAgent` 并实现 `run`**

```python
# agents/mymode/agent.py
from typing import Optional, Callable
from agents.base import BaseAgent


class MyModeAgent(BaseAgent):
    name = "my_mode"
    description = "我的执行模式"

    async def run(self, user_input: str, registry, memory, logger,
                  progress_callback: Optional[Callable] = None, **kwargs):
        """主执行入口。

        参数由框架注入：
        - registry   技能注册表，用于取工具
        - memory     记忆管理器
        - logger     结构化日志器
        - progress_callback  进度回调，用于向前端推送中间态
        """
        ...
```

注意依赖是**通过参数注入**的，不是 `self.xxx`。基类 `BaseAgent` 另提供：

- `on_startup()` —— 启动时初始化（默认清理过期缓存）
- `warmup_graph(logger)` —— 预热 LangGraph 图，避免首次请求冷启动慢
- `_enrich_user_input(...)` —— 用记忆/上下文增强用户输入
- `_update_intent_memory(...)` —— 回写意图到记忆

**2. 注册到 `AgentFactory`**

```python
# agents/__init__.py
AgentFactory.register(
    lambda: __import__("agents.mymode.agent", fromlist=["MyModeAgent"]).MyModeAgent(),
    name="my_mode",
)
```

用 `lambda` 延迟导入，避免启动期加载全部模式的依赖。

**3. 如用 LangGraph，参考现有实现**

`agents/plan/graph.py` 是最完整的参考：包含状态定义、节点、条件边、检查点。PDOR 模式（Plan-Do-Observe-Replan）见 `agents/pdor/graph.py`。

**4. 别忘了接入预算与追踪**

新模式必须接入预算控制，否则会失去成本护栏：

```python
from utils.budget import get_budget_controller, BudgetExceeded

controller = get_budget_controller()
controller.check(logger)        # 未超限返回 True；超限抛 BudgetExceeded
controller.add_tokens(count)    # 记录 token 消耗
controller.add_call()           # 记录一次 LLM 调用
```

预算由三维上限构成，可在 `.env` 调整：`MAX_TOKENS_PER_QUERY`、`MAX_LLM_CALLS_PER_QUERY`、`MAX_TIME_SECONDS`。超限后用户可选择继续，此时会开启短期豁免窗口（`BUDGET_EXEMPT_CALLS_LIMIT` / `BUDGET_EXEMPT_TIME_LIMIT`）。

追踪接入见 `utils/agent_trace/`，调用链会落到 SQLite，之后可用 CLI 的 `/trace` 命令回放。

---

## 扩展点四：新增报告模板

模板位于 `agents/report_templates/`，每个子目录一个模板，根目录 `index.json` 是注册表。

```
agents/report_templates/
├── index.json          # 模板注册表
├── blocks/             # 可复用内容块
├── earnings_analysis/  # 财报分析
├── industry_chain/     # 产业链
├── hot_news/           # 热点新闻
└── ...
```

新增步骤：

1. 复制一个最接近的模板目录
2. 修改其中的提示词与结构化定义
3. 在 `index.json` 中登记
4. 用 `.env` 的 `REPORT_TEMPLATE=<name>` 切换验证

模板选择逻辑见 `.env` 中的 `REPORT_TEMPLATE_CONTINUATION_MODE`（`keyword` 基于规则判定续问、零成本；`llm` 调用模型判定）。

---

## 扩展点五：新增通知渠道

通知层位于 `utils/notification/`，已支持钉钉、飞书、企业微信、Telegram、邮件（SMTP）、Discord、Slack、Pushover、ntfy、Gotify、PushPlus、Server酱3 共 12 种。

```
utils/notification/
├── base.py        # 渠道基类
├── channels/      # 各渠道实现
├── factory.py     # 渠道工厂
├── manager.py     # 统一出口
└── noise.py       # 去重 / 冷却 / 静默时段
```

新增步骤：

1. 在 `channels/` 下新建文件，继承 `BaseNotificationChannel`，实现抽象方法 `_do_send`：

```python
from utils.notification.base import (BaseNotificationChannel,
                                     NotificationMessage, NotificationResult)


class MyChannel(BaseNotificationChannel):
    name = "mychannel"

    def _do_send(self, message: NotificationMessage) -> NotificationResult:
        # message.title / message.content / message.format
        ...
        return NotificationResult(success=True, channel=self.name)

    def is_configured(self) -> bool:
        """未配置则返回 False，框架自动跳过该渠道"""
        ...
```

2. 在 `factory.py` 中注册渠道名
3. 在 `config.py` 中补充该渠道的配置项，并在 `.env.example` 中说明

基类 `BaseNotificationChannel` 已经封装好 `send()` 的通用流程（参数校验、异常兜底、结果包装），**你只需实现 `_do_send` 和 `is_configured`**。

去重、冷却、静默时段由 `noise.py` 统一处理，新渠道**不需要**自己实现。

---

## 扩展点六：新增回测策略

回测引擎基于 Backtrader，位于 `backtest/`：

```
backtest/
├── engine.py          # 引擎入口
├── strategy_base.py   # 策略基类
├── strategies/        # 具体策略
├── data_adapter.py    # 数据适配
├── analyzers.py       # 绩效分析
├── a_stock_broker.py  # A 股交易成本模型
└── dpo_indicator.py / cyw_indicator.py / dma_indicator.py  # 自定义指标
```

新增策略：继承 `strategy_base.py` 中的基类，实现 `next()`，放入 `strategies/`。

新增指标：参考 `dpo_indicator.py`（区间震荡线）或 `cyw_indicator.py`（主力控盘）的写法。

---

## 配置系统

配置走**三层**：

```
.env.example   模板（入库，全占位符）
     ↓ 复制
.env           你的实际配置（不入库，已在 .gitignore）
     ↓ 读取
config.py      Config 类，定义类型、默认值、校验规则
```

新增配置项的三步：

1. `.env.example` 中加注释与占位值
2. `config.py` 的 `Config` 类加属性，并登记到配置项元组（含类型与默认值）
3. 需要暴露到 Web 配置页的话，检查 `server/config_schema.py`

**约定**：

- 所有 Key 留空即禁用对应能力，不应导致启动失败
- 涉及 Key 的配置**永远不要**在日志、报告或 Web 接口中回显明文（已有掩码处理，新增时请保持一致）
- 数值型配置统一在 `config.py` 做 `int()` / `float()` 转换与边界处理

---

## 测试规范

```bash
pytest -m unit              # 纯逻辑，不联网，CI 必跑
pytest -m integration       # 需要网络
pytest -m e2e               # 需要 LLM API，会花钱
pytest -m "not e2e" -q      # 日常推荐
pytest test/unit/test_xxx.py::test_yyy   # 单条
```

**要求**：

- 新增数据源 / 技能 / 渠道 **必须**配套单元测试
- 单元测试不得依赖网络与外部服务（用 `monkeypatch` 打桩）
- 需要真实 API 的测试打上 `@pytest.mark.e2e` 或对应 marker，避免污染日常跑批
- 测试文件放在 `test/unit/` 下与源码对应的目录结构

marker 定义在 `pyproject.toml`：`unit` / `integration` / `e2e` / `slow` / `mx` / `tavily` / `hithink`。

---

## 代码规范

**通用**

- Python 3.11+ 语法，类型注解按需添加（关键接口必须有）
- 注释与文档字符串用中文，变量名用英文
- 单个函数超过 80 行考虑拆分

**日志**

- 统一用 `logging.getLogger("radar.<模块名>")`，不要 `print()`
- 敏感信息（Key、Token、密码）**绝不**写入日志

**错误处理**

- 边界失败返回结构化错误字典，含 `error` 与 `fallback` 字段，便于 LLM 决策
- 不要在库代码里 `sys.exit()` 或 `os._exit()`
- 数据源 / 外部调用必须有超时

**依赖**

- 新增第三方依赖必须写入 `requirements.txt` 并注明用途
- 重依赖（如 torch、chromadb）延迟导入，避免拖慢启动

**安全**

- 任何看起来像密钥的字符串，只能从 `.env` / 环境变量读取，不得硬编码
- 新代码提交前自查：有没有把本地绝对路径、个人 Key、内部地址写进去

---

## 提交 Pull Request

1. Fork 后建分支：`feat/xxx` / `fix/xxx` / `docs/xxx`
2. 确保 `pytest -m "not e2e"` 全绿
3. 提交信息写清楚"改了什么、为什么改"，不要只写"fix bug"
4. PR 描述中说明：改动范围、是否影响配置、是否需要新增依赖

---

## 许可证提示

本项目采用 **GPL-3.0**。你提交的 PR 将被视为同意以同等协议开源。

若你新增的代码引用了第三方项目，请在 PR 中说明来源与许可证，确保与 GPL-3.0 兼容（MIT / Apache-2.0 / BSD 可并入，专用协议需单独评估）。
