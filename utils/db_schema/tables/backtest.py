"""backtest.db 表定义（14 表 + 索引）。

DDL 逐字复制自 utils/cache/backtest_db.py::init_backtest_tables。
内置策略种子注册（backtest_db._register_builtin_strategies）由 registry 的 seeds 承接。
"""
from utils.db_schema.defs import TableDef

TABLES = [
    TableDef(name="data_tasks", ddl="""CREATE TABLE IF NOT EXISTS data_tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        config_json TEXT NOT NULL,
        total_count INTEGER DEFAULT 0,
        processed_count INTEGER DEFAULT 0,
        success_count INTEGER DEFAULT 0,
        failed_count INTEGER DEFAULT 0,
        current_code TEXT,
        current_name TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT,
        retry_count INTEGER DEFAULT 0,
        log_text TEXT,
        source TEXT,
        origin TEXT DEFAULT 'manual',
        ref_id TEXT DEFAULT '',
        duration_seconds REAL,
        updated_at TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_data_tasks_status ON data_tasks(status)",
                      "CREATE INDEX IF NOT EXISTS idx_data_tasks_created ON data_tasks(created_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_data_tasks_ref ON data_tasks(ref_id)"]),

    TableDef(name="strategies", ddl="""CREATE TABLE IF NOT EXISTS strategies (
        strategy_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        category TEXT NOT NULL,
        is_builtin INTEGER NOT NULL DEFAULT 1,
        code TEXT,
        params_schema TEXT NOT NULL,
        version TEXT DEFAULT '1.0',
        engine_version TEXT DEFAULT 'v1.0',
        author TEXT,
        tags TEXT,
        strategy_hash TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )"""),

    TableDef(name="backtest_runs", ddl="""CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',
        code TEXT NOT NULL,
        stock_name TEXT,
        strategy_id TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        params_json TEXT NOT NULL,
        config_json TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        initial_cash REAL NOT NULL DEFAULT 100000,
        commission REAL DEFAULT 0.0003,
        min_commission REAL DEFAULT 5.0,
        stamp_tax REAL DEFAULT 0.0005,
        transfer_fee REAL DEFAULT 0.00001,
        slippage REAL DEFAULT 0.001,
        t_plus_1 INTEGER DEFAULT 1,
        lot_size INTEGER DEFAULT 100,
        adjust_type TEXT DEFAULT 'qfq',
        stop_loss REAL DEFAULT 0,
        take_profit REAL DEFAULT 0,
        trailing_stop REAL DEFAULT 0,
        position_mode TEXT DEFAULT 'full',
        position_size REAL DEFAULT 1.0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        duration_seconds REAL,
        error_message TEXT,
        engine_version TEXT DEFAULT 'v1.0',
        origin TEXT DEFAULT 'manual',
        ref_id TEXT DEFAULT '',
        entry_timing TEXT DEFAULT 'next_open',
        exit_timing TEXT DEFAULT '',
        -- P0 撮合保真：涨跌停拦截口径快照。⚠️ 刻意不给 DEFAULT——旧行必须是 NULL
        -- （= 口径未记录），给了 DEFAULT 会把历史 run 伪装成新口径、新旧不可区分。
        -- 新 run 由 create_backtest_run 显式写 'enforce'（engine_version 同步升 v1.1）。
        price_limit_mode TEXT,
        updated_at TEXT,
        FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_bt_runs_code ON backtest_runs(code)",
                      "CREATE INDEX IF NOT EXISTS idx_bt_runs_strategy ON backtest_runs(strategy_id)",
                      "CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC)"]),

    TableDef(name="backtest_results", ddl="""CREATE TABLE IF NOT EXISTS backtest_results (
        run_id TEXT PRIMARY KEY,
        total_return REAL,
        annual_return REAL,
        benchmark_return REAL,
        excess_return REAL,
        sharpe_ratio REAL,
        sortino_ratio REAL,
        calmar_ratio REAL,
        max_drawdown REAL,
        max_drawdown_duration INTEGER,
        volatility REAL,
        downside_volatility REAL,
        trade_count INTEGER,
        win_count INTEGER,
        loss_count INTEGER,
        win_rate REAL,
        profit_factor REAL,
        avg_win REAL,
        avg_loss REAL,
        avg_holding_days REAL,
        max_consecutive_wins INTEGER,
        max_consecutive_losses INTEGER,
        final_equity REAL,
        peak_equity REAL,
        health TEXT DEFAULT 'ok',
        health_detail TEXT DEFAULT '',
        data_bars INTEGER DEFAULT 0,
        expected_bars INTEGER DEFAULT 0,
        coverage_pct REAL DEFAULT 0,
        gaps_json TEXT DEFAULT '[]',
        trade_sample_ok INTEGER DEFAULT 0,
        equity_curve_json TEXT,
        drawdown_curve_json TEXT,
        monthly_returns_json TEXT,
        -- P1 统计严谨（2026-10-15）：日频统计（PSR/DSR 输入）+ 分年拆解 + 多重检验校正
        sr_daily REAL,
        skewness REAL,
        kurtosis REAL,
        yearly_stats_json TEXT,
        deflated_sharpe REAL,
        trial_count INTEGER,
        engine_version TEXT DEFAULT 'v1.0',
        created_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
    )"""),

    TableDef(name="backtest_trades", ddl="""CREATE TABLE IF NOT EXISTS backtest_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        trade_no INTEGER NOT NULL,
        direction TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        price REAL NOT NULL,
        quantity INTEGER NOT NULL,
        amount REAL NOT NULL,
        commission REAL DEFAULT 0,
        stamp_tax REAL DEFAULT 0,
        transfer_fee REAL DEFAULT 0,
        slippage_cost REAL DEFAULT 0,
        total_cost REAL DEFAULT 0,
        pnl REAL,
        pnl_pct REAL,
        holding_days INTEGER,
        signal_reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id, trade_no)"]),

    TableDef(name="backtest_batch", ddl="""CREATE TABLE IF NOT EXISTS backtest_batch (
        batch_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        strategy_name TEXT,
        params_json TEXT,
        config_json TEXT NOT NULL,
        codes_json TEXT NOT NULL,
        code_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        avg_total_return REAL,
        avg_annual_return REAL,
        avg_sharpe REAL,
        avg_max_drawdown REAL,
        avg_win_rate REAL,
        profit_count INTEGER,
        loss_count INTEGER,
        initial_cash REAL,
        start_date TEXT,
        end_date TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        duration_seconds REAL,
        error_message TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_batch_status ON backtest_batch(status)",
                      "CREATE INDEX IF NOT EXISTS idx_batch_created ON backtest_batch(created_at DESC)"]),

    TableDef(name="backtest_batch_item", ddl="""CREATE TABLE IF NOT EXISTS backtest_batch_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        code TEXT NOT NULL,
        asset_type TEXT,
        run_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        total_return REAL,
        annual_return REAL,
        sharpe_ratio REAL,
        max_drawdown REAL,
        win_rate REAL,
        trade_count INTEGER,
        final_equity REAL,
        error_message TEXT,
        started_at TEXT,
        completed_at TEXT,
        UNIQUE(batch_id, code),
        FOREIGN KEY (batch_id) REFERENCES backtest_batch(batch_id)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_item_batch ON backtest_batch_item(batch_id)"]),

    TableDef(name="backtest_test_set", ddl="""CREATE TABLE IF NOT EXISTS backtest_test_set (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        codes_json TEXT NOT NULL,
        tags_json TEXT,
        code_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_test_set_name ON backtest_test_set(name)"]),

    TableDef(name="backtest_multi", ddl="""CREATE TABLE IF NOT EXISTS backtest_multi (
        multi_id TEXT PRIMARY KEY,
        strategy_ids_json TEXT NOT NULL,
        strategy_names_json TEXT NOT NULL,
        codes_json TEXT NOT NULL,
        code_count INTEGER NOT NULL,
        strategy_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        initial_cash REAL,
        start_date TEXT,
        end_date TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        duration_seconds REAL,
        error_message TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_multi_status ON backtest_multi(status)",
                      "CREATE INDEX IF NOT EXISTS idx_multi_created ON backtest_multi(created_at DESC)"]),

    TableDef(name="backtest_multi_item", ddl="""CREATE TABLE IF NOT EXISTS backtest_multi_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        multi_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        code TEXT NOT NULL,
        batch_id TEXT,
        run_id TEXT,
        total_return REAL,
        annual_return REAL,
        sharpe_ratio REAL,
        max_drawdown REAL,
        win_rate REAL,
        trade_count INTEGER,
        max_consecutive_losses INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        completed_at TEXT,
        UNIQUE(multi_id, strategy_id, code)
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_multi_item_multi ON backtest_multi_item(multi_id)"]),

    TableDef(name="strategy_run", ddl="""CREATE TABLE IF NOT EXISTS strategy_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_uuid TEXT UNIQUE NOT NULL,
        idea TEXT NOT NULL,
        universe TEXT NOT NULL,
        period TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        stop_reason TEXT,
        best_iter_id INTEGER,
        total_iters INTEGER DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'panel',
        dialog_uuid TEXT,
        -- C 批：策略研发自建的 agent_trace run_id。
        -- 之前策略轮不进 LangGraph 图 → 没有 trace run → 研发期 8 轮 LLM 调用在
        -- 月历/token 统计里凭空消失，只能靠「猜最近一条 run」兜底（分库跨月回退
        -- 会命中文档性陈旧 running 行 → 过程流显示上周的周报）。显式落库即根除。
        trace_run_id TEXT DEFAULT '',
        -- 年化/指标口径版本：2026-10-18 起年化由「rnorm100（按 bar 数 252/年）」
        -- 改为「自然日 CAGR + 非日线判空」。不标记的话，同一策略在口径切换前后的
        -- 年化不可比，用户会以为结论变了。老记录为空串 = 旧口径。
        engine_version TEXT NOT NULL DEFAULT '',
        -- 买卖**各自**的成交口径（之前只在 strategy_session 里存，面板/技能起的任务
        -- 落不了库 → 续跑与结果卡只能靠猜）。三档枚举同 strategy_session：
        -- next_open 次一 bar 开盘 / same_close 同 bar 收盘（尾盘）/ next_close 次一 bar 收盘。
        -- exit_timing 空串 = 跟随 entry_timing（最常见默认，不写死成 next_open）
        entry_timing TEXT NOT NULL DEFAULT 'next_open',
        exit_timing TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        completed_at TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_sr_status ON strategy_run(status)",
                      "CREATE INDEX IF NOT EXISTS idx_sr_created ON strategy_run(created_at DESC)"]),

    TableDef(name="strategy_iter", ddl="""CREATE TABLE IF NOT EXISTS strategy_iter (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES strategy_run(id),
        round INTEGER NOT NULL,
        hypothesis TEXT,
        strategy_json TEXT,
        strategy_hash TEXT,
        strategy_diff TEXT,
        compile_ok INTEGER NOT NULL,
        compile_error TEXT,
        is_metrics TEXT,
        oos_metrics TEXT,
        overfit_flag INTEGER DEFAULT 0,
        verdict TEXT,
        model TEXT,
        created_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_iter_run ON strategy_iter(run_id, round)",
                      "CREATE INDEX IF NOT EXISTS idx_iter_hash ON strategy_iter(strategy_hash)"]),

    # strategy_session（对话式策略研发会话，Plan 2）
    # 字段口径：
    # - status：pending_confirm（槽位已按推荐值补全，一行回测都没跑）/ active（用户已确认，允许创建 strategy_run）
    #           / archived（会话结束）
    # - opened_by：explicit（前端显式入口）/ rule（白盒词表命中）/ llm（LLM 兜底）
    # - entry_timing / exit_timing：买/卖**各自**的成交口径。三档枚举：
    #   next_open（次一 bar 开盘）/ same_close（同 bar 收盘，即「尾盘」）/ next_close（次一 bar 收盘）。
    #   两者分开是「尾盘买入、次日卖出」这类需求能被真实表达的前提
    # - slots_json：{"universe": {"value": "...", "source": "user|recommended|default"}, ...}
    #               记住每个槽位是用户给的还是系统补的（结果卡要如实标注）
    TableDef(name="strategy_session", ddl="""CREATE TABLE IF NOT EXISTS strategy_session (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_uuid TEXT UNIQUE NOT NULL,
        dialog_uuid TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_confirm',
        src_text TEXT DEFAULT '',
        idea TEXT DEFAULT '',
        universe TEXT DEFAULT '',
        period_start TEXT DEFAULT '',
        period_end TEXT DEFAULT '',
        entry_text TEXT DEFAULT '',
        exit_text TEXT DEFAULT '',
        entry_timing TEXT DEFAULT 'next_open',
        exit_timing TEXT DEFAULT '',
        slots_json TEXT DEFAULT '{}',
        plan_json TEXT DEFAULT '{}',
        opened_by TEXT DEFAULT '',
        intent_confidence REAL DEFAULT 0,
        intent_signals TEXT DEFAULT '{}',
        run_uuid TEXT DEFAULT '',
        turn_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_ss_dialog ON strategy_session(dialog_uuid, status, updated_at DESC)",
                      "CREATE INDEX IF NOT EXISTS idx_ss_status ON strategy_session(status)"]),

    # backtest_eval（样本内外评判，Plan 3 任务 2）
    # verdict 取值：ok（样本内外一致）/ overfit_suspect（IS/OOS 夏普比 > 1.3）
    #              / insufficient_sample（两段中较少的那段成交 < 10 笔）
    #              / oos_negative（OOS 夏普 < 0）/ failed（评判本身失败，错误文本进 error_message）
    # ⚠️ 与 backtest_results.health 是两件事：health 看数据够不够，verdict 看结论经不经得起样本外检验
    TableDef(name="backtest_eval", ddl="""CREATE TABLE IF NOT EXISTS backtest_eval (
        run_id TEXT PRIMARY KEY,
        is_metrics_json TEXT,
        oos_metrics_json TEXT,
        split_date TEXT,
        is_sharpe REAL,
        oos_sharpe REAL,
        sharpe_ratio_io REAL,
        overfit_flag INTEGER NOT NULL DEFAULT 0,
        verdict TEXT NOT NULL DEFAULT '',
        eval_run_ids_json TEXT DEFAULT '[]',
        error_message TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_bt_eval_verdict ON backtest_eval(verdict, overfit_flag)"]),

    # ── 因子评估报告（工作台 v3 · F1 持久化）─────────────────────────
    # payload（3~12MB JSON）不进库，落 output/factor_reports/<uuid>.json；
    # 库里只存摘要（kpi_json）与路径，列表页只读摘要，详情页按需读文件。
    # 缓存版结果（_CACHE TTL 30min 重启即丢）的教训：评估结果必须可回看。
    TableDef(name="factor_report", ddl="""CREATE TABLE IF NOT EXISTS factor_report (
        report_uuid TEXT PRIMARY KEY,
        title TEXT,
        index_code TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        params_json TEXT NOT NULL,
        kpi_json TEXT,
        payload_path TEXT,
        html_path TEXT,
        n_factors INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'done',
        created_at TEXT NOT NULL
    )""",
             indexes=["CREATE INDEX IF NOT EXISTS idx_factor_report_created "
                      "ON factor_report(created_at DESC)"]),
]
