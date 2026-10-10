# scripts/ —— 维护与工具脚本

> 全部脚本都从**仓库根**执行：`python scripts/<组>/<脚本>.py`
> 解释器优先 `./.venv`，其次 `./venv`，最后系统 `python`（同 AGENTS.md）。

## 目录约定（按生命周期，不按"谁写的"）

| 目录 | 含义 | 判据 |
|---|---|---|
| `ops/` | **日常运维** | 可反复执行；动本地状态（库 / 日志 / 缓存 / 画像） |
| `data/` | **运行期数据生成** | **产出被 runtime 读**，删了就没有再生手段 |
| `devtools/` | **研发与验收** | 离线、不进生产路径；压缩 / 记忆 / 策略的回归与研究 |
| `archive/` | **一次性脚本留档** | 场景已消失，重跑无意义；留档供追溯，**不再维护** |

新增脚本先问自己：**「这个动作还能再跑吗？」** → 能，进 `ops/` 或 `devtools/`；不能（针对某次特定事故/版本），进 `archive/` 并在头部标注执行日期。

---

## ops/ —— 日常运维（13）

| 脚本 | 用途 | 默认行为 | 用法 |
|---|---|---|---|
| `init_dbs.py` | 全部业务库表结构对齐（缺表建/缺列补，加性幂等） | **直接 apply** | `[--check] [--db a,b] [--list] [--verbose]` |
| `refresh_background_data.py` | 跑 bootstrap 的后台作业（不启 web） | 跑全部默认作业 | `--only shortline_refresh` |
| `patch_iwencai_skill_contracts.py` | 给问财技能包补齐注册契约段 | **直接写 18 个 SKILL.md** | `--check` 只报告差异 |
| `migrate_trace_monthly.py` | agent_trace 按月分库迁移（幂等可续跑，被单测锁定） | **预演** | `--apply`（需先停 web/CLI） |
| `backfill_index_tech_panels.py` | 把历史报告里误判成个股的指数图板回填为 `market=INDEX` | **预演** | `--apply [--sync] [--force] [--codes]` |
| `backfill_memory_chunks.py` | 记忆 chunk 元数据（index/count/group）存量回填 | 执行（自动备份） | `[--dry-run] [--force] [--no-backup]` |
| `repair_memory_confidence.py` | 修存量记忆 confidence 并把 Chroma 与 SQLite 对齐 | **dry-run** | `--apply [--skip-names]` |
| `clean_profile_watched_stocks.py` | 清理画像 `watched_stocks`/`recent_queries` 的污染键 | **预演** | `--apply [--keep k] [--drop-codes c]` |
| `clean_pycache.py` | 递归删 `__pycache__` | 直接删 | — |
| `clean_cache.py` | 清过期缓存（`utils.cache.async_clean_expired_cache`） | 直接清 | — |
| `clean_logs_and_db.py` | 删小日志 + dialog 孤儿记录 + 过期缓存 | **直接删** ⚠️ 见下 | — |
| `analyze_logs_cleanup.py` | 按**保留期**分析并清理日志（含系统日志按行裁剪） | **预演** | `--execute [--keep-days N] [--force-old]` |
| `analyze_log.py` | 深度诊断单个 agent 日志（阶段耗时/工具压缩/上下文压缩/循环检测） | 只读 | `<log_path> [log_path2 ...]` |

## data/ —— 运行期数据生成（2）

| 脚本 | 产出 | 消费者 | 用法 |
|---|---|---|---|
| `gen_trading_calendar.py` | `data/trading_calendar.json` | `server/calendar_service.py` | `[年份]` / `[起 止]`，默认当年 |
| `generate_questions.py` | `data/suggested_questions.json` | `utils/app_paths.get_suggested_questions_path()` | `[--count 30] [--replace]` |

> ⚠️ 这两个脚本的产出**被运行时直接读取**，不是"生成一次就完事的工具"。改 `data/*.json` 前先看这里。

## devtools/ —— 研发与验收（6）

| 脚本 | 用途 | 用法 |
|---|---|---|
| `export_tool_corpus.py` | 从 agent_trace 导出工具压缩语料（train/test） | `[--stats-only] [--quota-compressed N] [--tools a,b]` |
| `tool_compress_replay.py` | 离线回放压缩器并输出指标 | `[--compressor mod:fn] [--judge 20] [--out f.md]` |
| `tool_compress_shadow_report.py` | 生成影子对照报告（原始 / LLM / 规则） | `[--tool X] [--date YYYYMMDD] [--format csv] [--out f]` |
| `memory_backtest.py` | 记忆回测：分词 / 片段完整性 / 召回（**只读源库**） | `[--limit 40] [--with-embedding] [--top-k 5]` |
| `memory_rebuild.py` | 记忆库重建 + 端到端回测 | `[--dry-run] [--limit 20] [--no-backup]` |
| `backtest_co_exposure.py` | co_exposure「整体降仓 vs 内部调权」对比回测（规则验证模板） | `[--json out.json]` |

## archive/ —— 一次性脚本留档（5，不再维护）

| 脚本 | 当时做的事 |
|---|---|
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

## ⚠️ 已知问题：`devtools/export_tool_corpus.py` 已失效（2026-09-23 trace 分库之后）

`agent_trace.db` 按月分库后，**主库只剩 `reviews`**，`runs/steps/messages` 移到了
`db/agent_trace/agent_trace_YYYYMM.db`。而该脚本的 `_resolve_dbs()` 返回
`[主库] + 全部月库`，`eval/tool_corpus.py::build_records()` 却对第一个库无条件查 `steps`
→ `sqlite3.OperationalError: no such table: steps`，导出直接中断
（连带 `tool_compress_replay.py` 的语料输入断供）。

**修法（未改，待确认）**：在 `build_records()` 前跳过缺 `steps` 的库，例如
`if "steps" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}: continue`。

---

## 维护约定（新增/移动脚本时必看）

1. **仓库根锚点**：脚本从根执行，取仓库根统一用
   `Path(__file__).resolve().parents[2]`（或 3 层 `os.path.dirname(...)`）。
   *曾经的坑*：`scripts/x.py` 只用 2 层 → 一移进子目录，`sys.path` 就指错（`clean_cache.py` 当年就因此失效）。
2. **破坏性操作默认 dry-run**，落盘用 `--apply` / `--execute`；写文件前自动备份 + 留痕（`repaired_from` 之类）。
3. **移动脚本必须同步 3 类引用**：
   - 单测按路径加载的：`test/unit/thesis/test_co_exposure_backtest.py`（→ `scripts/devtools/`）、`test/unit/trace/test_migrate_trace_monthly.py`（→ `scripts/ops/`）；
   - `patch_iwencai_skill_contracts.py` 的 `MARKER` 常量**内嵌自身路径**，同时写进 18 个 `tools/other_skills/10jqka/*/SKILL.md` → 移动后必须同步这 19 处，否则重跑会全量重写；
   - 文档：`AGENTS.md`（Maintenance 段）、`README.md`（目录树 + 维护脚本段）、`CODE_WIKI.md`、`docs/**`。
4. **同目录兄弟 import 不可拆**：`memory_rebuild.py` 直接 `from memory_backtest import ...` → 两者必须留在同一目录。
5. `test/scripts/` 已于 2026-10-01 撤销（它的内容其实是运维脚本与运行期数据生成器，放 `test/` 下语义错位；pytest 也不收集它们）。

## 已移除

| 脚本 | 原因 |
|---|---|
| `clean_profile_pollution.py` | 规则是 `ops/clean_profile_watched_stocks.py` 的子集，已被取代（2026-10-01） |
