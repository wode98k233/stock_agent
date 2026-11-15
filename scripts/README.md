# scripts/ —— 维护与工具脚本

> 全部脚本都从**仓库根**执行：`python scripts/<组>/<脚本>.py`
> 解释器优先 `./.venv`，其次 `./venv`，最后系统 `python`（同 AGENTS.md）。

## 目录约定（按生命周期，不按"谁写的"）

| 目录 | 含义 | 判据 |
|---|---|---|
| `ops/` | **日常运维** | 可反复执行；动本地状态（库 / 日志 / 缓存 / 画像） |
| `data/` | **运行期数据生成** | **产出被 runtime 读**，删了就没有再生手段 |
| `devtools/` | **研发与验收** | 离线、不进生产路径；压缩 / 记忆 / 策略的回归与研究 |
| `devtools/_probes/` | **探针实现模块（私有）** | 不直接运行；由 `devtools/` 顶层入口脚本（带子命令）调用 |
| `archive/` | **一次性脚本留档** | 场景已消失，重跑无意义；留档供追溯，**不再维护** |

新增脚本先问自己：**「这个动作还能再跑吗？」** → 能，进 `ops/` 或 `devtools/`；不能（针对某次特定事故/版本），进 `archive/` 并在头部标注执行日期。

> ⚠️ `devtools/` 里**同族探针已合并为带子命令的统一入口**（如 `probe_prefix.py <子命令>`），实现代码保留在同目录 `_probes/` 下（逻辑零改动）。新增探针优先并入既有入口的子命令，而非再开新文件。

---

## ops/ —— 日常运维（23）

| 脚本 | 用途 | 默认行为 | 用法 |
|---|---|---|---|
| `init_dbs.py` | 全部业务库表结构对齐（缺表建/缺列补，加性幂等） | **直接 apply** | `[--check] [--db a,b] [--list] [--verbose]` |
| `refresh_background_data.py` | 跑 bootstrap 的后台作业（不启 web） | 跑全部默认作业 | `--only shortline_refresh` / `--only market_amount_refresh`（收盘后补录全市场成交额，非交易日与收盘前自动跳过）|
| `patch_iwencai_skill_contracts.py` | 给问财技能包补齐注册契约段 | **直接写 18 个 SKILL.md** | `--check` 只报告差异 |
| `migrate_trace_monthly.py` | agent_trace 按月分库迁移（幂等可续跑，被单测锁定） | **预演** | `--apply`（需先停 web/CLI） |
| `compact_trace_blobs.py` | **存量 trace 库瘦身回填**（T1 摘要化 + T2 内容寻址压缩）：阶段 A 把历史 `steps.input/output` 改写成摘要，阶段 B 把长文本压进 `blobs`。幂等；每次 apply 前用 SQLite 在线备份 API 生成 `<db>.bak-<ts>`；探锁失败直接拒绝 | **预演** | `--apply [--month 202610] [--db 路径] [--no-vacuum]`（⚠️ **必须先停 web/TUI**：旧进程没有透明解压，会读到哨兵串） |
| `backfill_index_tech_panels.py` | 把历史报告里误判成个股的指数图板回填为 `market=INDEX` | **预演** | `--apply [--sync] [--force] [--codes]` |
| `backfill_costs.py` | 给上线前的 LLM 步骤补 `steps.extra.cost`（按**当前价目**估算，标注 `recomputed`） | **预演** | `--apply [--days N] [--db 月库]` |
| `backfill_memory_chunks.py` | 记忆 chunk 元数据（index/count/group）存量回填 | 执行（自动备份） | `[--dry-run] [--force] [--no-backup]` |
| `repair_memory_confidence.py` | 修存量记忆 confidence 并把 Chroma 与 SQLite 对齐 | **dry-run** | `--apply [--skip-names]` |
| `clean_profile_watched_stocks.py` | 清理画像 `watched_stocks`/`recent_queries` 的污染键 | **预演** | `--apply [--keep k] [--drop-codes c]` |
| `clean_pycache.py` | 递归删 `__pycache__` | 直接删 | — |
| `check_tool_descriptions.py` | 工具 description 体检：长度分布 / 业务口径混入 / 参数枚举豁免（PRD-A9 §2.3.2 迁移护栏，只读） | 只报告 | `[--strict]`（有待拆分项时退出码 1，可接 CI） |
| `clean_cache.py` | 清过期缓存（`utils.cache.async_clean_expired_cache`） | 直接清 | — |
| `clean_logs_and_db.py` | 删小日志 + dialog 孤儿记录 + 过期缓存 | **直接删** ⚠️ 见下 | — |
| `analyze_logs_cleanup.py` | 按**保留期**分析并清理日志（含系统日志按行裁剪） | **预演** | `--execute [--keep-days N] [--force-old]` |
| `analyze_log.py` | 深度诊断单个 agent 日志（阶段耗时/工具压缩/上下文压缩/循环检测） | 只读 | `<log_path> [log_path2 ...]` |
| `backfill_strategy_hash.py` | 回填 `strategies.strategy_hash`：存量「研发→采纳」策略的指纹补写 | — | — |
| `capability_proposals.py` | 能力清单 / 扩展提议的运维入口（AI 出方案 → 人审） | — | — |
| `run_factor_eval.py` | 批量因子评估（36 因子 IC / 分层 + DSR；落 `factor_report` 报告） | 执行 | `[--index 000300] [--start] [--end] [--factors a,b] [--out] [--json-out] [--no-html]` |
| `compare_factor_pools.py` | 因子**跨池**稳健性对照（产出 `output/factor_pool_compare_*.json`，供 `build_factor_prior` 用） | 执行 | `[--pools 000300,000905] [--start] [--end] [--neutralize …]` |
| `build_factor_prior.py` | 由已落库报告 + 跨池对照生成先验 `output/factor_prior.json`（策略研究注入用） | 执行（写先验文件） | `[--print]` |
| `backfill_index_universe.py` | 回补指数成分股日线到 `market_data.db`（因子截面评估的数据前置；成分快照落库 `index_constituent`，断点续跑） | 执行 | `[--dry-run] [--limit N] [--index 000300] [--days N] [--codes a,b]` |
| `enrich_universe_meta.py` | 补齐成分权重 + 行业元数据（行业中性化 / 权重前置） | 执行 | `[--weights 000300,000905] [--industry] [--all]` |

## data/ —— 运行期数据生成（2）

| 脚本 | 产出 | 消费者 | 用法 |
|---|---|---|---|
| `gen_trading_calendar.py` | `data/trading_calendar.json` | `server/calendar_service.py` | `[年份]` / `[起 止]`，默认当年 |
| `generate_questions.py` | `data/suggested_questions.json` | `utils.app_paths.get_suggested_questions_path()` | `[--count 30] [--replace]` |

> ⚠️ 这两个脚本的产出**被运行时直接读取**，不是"生成一次就完事的工具"。改 `data/*.json` 前先看这里。

## devtools/ —— 研发与验收（20）

> 同族探针合并为带子命令的入口（见下 4 个 `*.py`），实现保留在 `devtools/_probes/`。

| 脚本 | 用途 | 用法 |
|---|---|---|
| `probe_prefix.py` | **前缀族探针统一入口**。子命令：`report-cache`（静态 system 前缀缓存与 TTL，PRD-A9）/ `cache-by-node`（按 node/label 聚合命中与成本，PRD-A9/A10）/ `shape`（主前缀分段 + 字节稳定性 + fork 前后成本对照，PRD-A11）/ `layout`（L0/L1/L2 布局命中对照，`--dry-run` 不联网）/ `monotonicity`（逐节点前缀单调性，只读 trace） | `probe_prefix.py <子命令> [参数]` |
| `probe_compaction.py` | **上下文压缩族探针统一入口**。子命令：`cost`（压缩峰值 / 无压缩峰值 / 输入输出构成 / 收费情景 / 预算占用，PRD-A11）/ `impact`（按 run 统计压缩次数 / 命中率 / 峰值 / 预算占比，PRD-A10）/ `requery`（压缩次数 × 完全重复工具调用率，按 `tool_call_id` 去重，PRD-A11 §10） | `probe_compaction.py <子命令> [参数]` |
| `verify_report_fork.py` | **报告链路 fork 族验证入口**。子命令：`shape`（三条调用是否同前缀族 + 5 个 builder 的 system 字节一致性，零 LLM）/ `order`（直接量 `report-fast` 命中率 ≥50% 健康） | `verify_report_fork.py <子命令> [参数]` |
| `probe_shared_block_prefix.py` | **共享块前缀族入口**。子命令：`classify`（classify/clarify 消息形态与共享前缀）/ `pdor`（PDOR planner，同上）；均 `--record` 记基线 / `--check` 断言不变性 | `probe_shared_block_prefix.py <子命令> [参数]` |
| `export_tool_corpus.py` | 从 agent_trace 导出工具压缩语料（train/test） | `[--stats-only] [--quota-compressed N] [--tools a,b]` |
| `tool_compress_replay.py` | 离线回放压缩器并输出指标 | `[--compressor mod:fn] [--judge 20] [--out f.md]` |
| `tool_compress_shadow_report.py` | 生成影子对照报告（原始 / LLM / 规则） | `[--tool X] [--date YYYYMMDD] [--format csv] [--out f]` |
| `probe_selector_compact_risk.py` | **select_skills 紧凑化的离线风险评估**（零 LLM 调用）：两项结构性证据覆盖全部模板 —— ①技能名是否只在 `sections[].prompt` 正文里出现、②正文是否含指向技能/数据源的措辞。两项成立即**无需真实 LLM 的 A/B**（PRD-A11 FR-5.1） | `[--verbose]` |
| `verify_context_summary_shape.py` | **压缩 fork 形态验证**（零 LLM 调用）：跑一次真实压缩流程，打出模型 / tools / 前缀共享条数 / 尾部长度，逐条核对「前缀共享 + 尾部追加」 | `[--mode fork\|standalone] [--recent N] [--rounds N]` |
| `verify_e2e_modes.py` | **端到端多模式验证（测量台）**：同一问题在 react_stock / unified_plan 各跑一次，再逐 step 对照命中率与 fork 形态（⚠️ 会真实调用 LLM）。**与 `test/e2e/test_all_modes_e2e.py` 的分工**：断言归那边（CI 可跑），"逐 step 命中率表"归这边（人看，不产出断言） | `["问题"] [--modes a,b] [--no-run]` |
| `verify_context_summary_live.py` | **压缩 fork 的真实 LLM 验证**（⚠️ **会花钱**，每次约 ¥0.05）：预热 → 压缩，直接量出 provider 侧命中率。`--run` 才真调用，默认 dry-run。取 usage 必须拦 `utils.llm_factory.tracked_invoke` | `[--run] [--rounds 20]` |
| `memory_backtest.py` | 记忆回测：分词 / 片段完整性 / 召回（**只读源库**） | `[--limit 40] [--with-embedding] [--top-k 5]` |
| `strategy_session_smoke.py` | 策略会话端到端冒烟：开对话 → 说想法 → 答澄清卡 → 起跑 → 看结果（链路：闸门 → 澄清槽位 → 会话执行器 → 研发循环 → 结果卡） | — |
| `memory_rebuild.py` | 记忆库重建 + 端到端回测 | `[--dry-run] [--limit 20] [--no-backup]` |
| `backtest_co_exposure.py` | co_exposure「整体降仓 vs 内部调权」对比回测（规则验证模板） | `[--json out.json]` |
| `context_insight_probe.py` | **上下文洞察三个实测数字的只读复算**（AGENTS.md / CODE_WIKI §11.18 的 92.3% / 66.1% / 4.24×）：逐月打思考占输出、ΣLLM/墙钟 逐 run 中位、平铺/墙钟 极值与「步数最多的 run」 | `[--month 202610] [--all] [--top N] [--json]` |
| `check_config_parity.py` | **config/ 拆包属性 parity**：捕获基线 / 对比前后标量类属性（拆包零改动护栏） | `capture <out.json>` \| `check <before.json> <after.json>` |
| `probe_skill_guide_coverage.py` | **工具→技能使用指南覆盖率只读探针**（不跑 LLM）：量化「调了未被选中技能的工具」= 无使用指南 | `[--sample N] [--json out.json]` |
| `verify_clarify_lean_input.py` | **澄清反问精简块路径真机验证**（⚠️ `--run` 才真调 LLM；默认预演不花钱） | `[--run] [--fallback "…"]` |
| `probe_trace_size.py` | **Trace 存储体积只读探针**：逐库/逐字段打 UTF-8 字节、按 step_type 与 messages.role 的重复率、4KB 块内容寻址 + zlib 的「理论下限」，以及瘦身目标模型（`messages + steps.output`）的预估体积。⚠️ 口径必须用 `length(cast(x AS blob))`，`length(TEXT)` 数字符会把体积低估 2.6~3 倍 | `[--month 202610] [--no-compress] [--json out.json]` |

## archive/ —— 一次性脚本留档（19，不再维护）

| 脚本 | 当时做的事 |
|---|---|
| `check_group_report_input.py` | Group 报告层「有没有拿到原始数据」契约自检（2026-10-02 速览模式排查） |
| `p1_demo_multi_window.py` | 回测 P1 统计严谨性的全链验收演示（多窗口 + Deflated Sharpe + 分年拆解） |
| `probe_tool_surface_cache.py` | 工具面形态（full/strict）的缓存对照探针（PRD-A9 §2）；其结论已被 `devtools/probe_prefix.py shape` 与 `probe_tools_cache_key.py` 覆盖 |
| `probe_prefix_sizes.py` | 前缀各段体量（tools / agent system / 技能目录 / 模板 JSON / selector system）；已被 `devtools/probe_prefix.py shape`（超集）取代（2026-10-17） |
| `verify_report_system.py` | 5 条 builder 的 system 字节一致性；已并入 `devtools/verify_report_fork.py shape` 的「判据③」（2026-10-17） |
| `verify_dashboard_fork.py` | dashboard fork 的最小真实跑；与 `devtools/verify_e2e_modes.py --modes unified_plan` 重复，且其声称的"命中率 ≥50%"校验**从未实现**（2026-10-17） |
| `probe_agent_payload.py` | agent 首调 payload 段构成研究（PRD-A10 已结题）；口径已并入 `probe_prefix.py shape`（2026-10-17） |
| `probe_selector_stability.py` | select_skills system 字节稳定性研究（PRD-A10 已结题）；稳定性校验见 `probe_prefix.py shape` 第 2 节（2026-10-17） |
| `probe_tools_cache_key.py` | 判定 tools 数组是否参与缓存键（PRD-A10 §2.5.1 **决定性实验**，结论已入册，2026-10-18 归档） |
| `mark_legacy_sharpe.py` | 把「旧口径夏普」标注为不可用（一次性运维，2026-10-18 归档） |
| `quantify_price_limit_delta.py` | 涨跌停/滑点修正的净影响量化（写死样本，2026-10-18 归档） |
| `clean_stock_daily_fixture.py` | 清 `market_data.db` 里的测试 fixture 并重取被覆盖的真实 bar（2026-10-18 归档） |
| `purge_non_daily_rows.py` | 清 `stock_daily` 里的非日线污染快照（2026-10 事故善后，2026-10-18 归档） |
| `clean_test_trace_runs.py` | 一次性清理：删除单测写进 trace 月库的 `ss-test` 痕迹（2026-10-15 事故） |
| `migrate_dbs_to_db_dir.py` | 把仓库根散落的 `*.db`（含 -wal/-shm）搬进 `db/` |
| `migrate_eval_v2.py` | Eval 评测体系 v1 → v2 历史数据迁移 |
| `backfill_v2.py` | 记忆系统 v2：按来源回填 confidence + trace_run_id |
| `qa_verify_eastmoney.py` | 东财持仓同步的 QA 第三轮对抗验证 |
| `clean_and_test_rating.py` | 一次性调试脚本（清理缓存并测评级获取） |

---

## 日志 / 缓存清理：两个入口并存（**用途不同，不合并**）

| 脚本 | 想要什么时用它 | 判据 | 默认行为 |
|---|---|---|---|
| `analyze_logs_cleanup.py` | **只删过期日志**（保留期保护） | 超过 `--keep-days`（默认 14 天）才可清；系统日志按行时间戳裁剪 | 预演（`--execute` 才落盘） |
| `clean_logs_and_db.py` | **清碎片 + 收尾**：小节日志、dialog 孤儿记录、过期缓存一起处理 | 行数 < 200 的日志文件（不看时间） | **直接删** ⚠️ 无 dry-run |

两者判据不同是**分工而非冲突**，按需选用；但**别在同一天盲目跑两遍**。

**推荐姿势**：先 `python scripts/ops/analyze_logs_cleanup.py` 预演看清"按保留期该删什么"，
再决定要不要追加 `clean_logs_and_db.py` 收尾（它会删掉保留期内但**内容极少**的日志文件 —— 先确认这些不是你还要留的）。

> 另注：`clean_logs_and_db.py` 的第 4 步与 `clean_cache.py` 做的是**同一件事**
> （都调 `utils.cache.async_clean_expired_cache`），跑其中一个即可，不必都跑。

## ✅ 已修复：`devtools/export_tool_corpus.py`（2026-10-17）

**症状**（2026-09-23 trace 按月分库后）：`agent_trace.db` 主库只剩 `reviews`，
`runs/steps/messages` 都在 `db/agent_trace/agent_trace_YYYYMM.db`。而脚本的 `_resolve_dbs()`
返回 `[主库] + 全部月库`，`eval/tool_corpus.py::build_records()` 对第一个库无条件查 `steps`
→ `sqlite3.OperationalError: no such table: steps`，导出中断（连带 `tool_compress_replay.py` 语料断供）。

**修法**：在 `_resolve_dbs()` 里按「库里有没有 `steps` 表」过滤（新增 `_has_steps_table()`），
主库被自动跳过 —— 比在 `build_records()` 里跳过更早、且不必打开坏库。
实测：主库 `_has_steps_table=False`，过滤后导出 5 个月库成功。

---

## 维护约定（新增/移动脚本时必看）

1. **仓库根锚点**：脚本从根执行，取仓库根统一用
   `Path(__file__).resolve().parents[2]`（或 3 层 `os.path.dirname(...)`）。
   ⚠️ **例外**：`scripts/devtools/_probes/` 下的实现模块多一级，用 `parents[3]`。
   *曾经的坑*：`scripts/x.py` 只用 2 层 → 一移进子目录，`sys.path` 就指错（`clean_cache.py` 当年就因此失效）。
2. **破坏性操作默认 dry-run**，落盘用 `--apply` / `--execute`；写文件前自动备份 + 留痕（`repaired_from` 之类）。
3. **移动脚本必须同步 3 类引用**：
   - 单测按路径加载的：`test/unit/thesis/test_co_exposure_backtest.py`（→ `scripts/devtools/`）、`test/unit/trace/test_migrate_trace_monthly.py`（→ `scripts/ops/`）；
   - `patch_iwencai_skill_contracts.py` 的 `MARKER` 常量**内嵌自身路径**，同时写进 18 个 `tools/other_skills/10jqka/*/SKILL.md` → 移动后必须同步这 19 处，否则重跑会全量重写；
   - 文档：`AGENTS.md`（Maintenance 段）、`README.md`（目录树 + 维护脚本段）、`CODE_WIKI.md`、`docs/**`。
4. **同目录兄弟 import 不可拆**：`memory_rebuild.py` 直接 `from memory_backtest import ...` → 两者必须留在同一目录；`_probes/probe_pdor_planner_prefix.py` 直接 `from probe_classify_clarify_prefix import ...` → 两者同在 `_probes/`（靠 `_probes` 在 `sys.path`），不可拆。
5. `test/scripts/` 已于 2026-10-01 撤销（它的内容其实是运维脚本与运行期数据生成器，放 `test/` 下语义错位；pytest 也不收集它们）。

## 已移除

| 脚本 | 原因 |
|---|---|
| `clean_profile_pollution.py` | 规则是 `ops/clean_profile_watched_stocks.py` 的子集，已被取代（2026-10-01） |
