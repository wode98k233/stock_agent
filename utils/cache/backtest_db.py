"""backtest.db 连接管理和表初始化

职责：
- data_tasks: 数据采集任务管理
- strategies: 策略定义（内置+用户自定义）
- backtest_runs: 回测任务
- backtest_results: 回测结果（统计指标+曲线数据）
- backtest_trades: 交易记录
"""
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from config import Config
from utils.cache.db_utils import make_db_context

get_backtest_db = make_db_context(lambda: Config.get_backtest_db_path())


def init_backtest_tables():
    """初始化 backtest.db 表结构"""
    with get_backtest_db() as conn:
        # 启用 WAL 模式：允许并发读写，避免批量回测多线程写 DB 时锁冲突
        conn.execute("PRAGMA journal_mode = WAL")
        c = conn.cursor()

        # -----------------------------------------------------------
        # 1. 数据采集任务管理
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS data_tasks (
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
            duration_seconds REAL,
            updated_at TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_data_tasks_status ON data_tasks(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_data_tasks_created ON data_tasks(created_at DESC)')

        # -----------------------------------------------------------
        # 2. 策略定义
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS strategies (
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
            created_at TEXT NOT NULL,
            updated_at TEXT
        )''')

        # -----------------------------------------------------------
        # 3. 回测任务
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_runs (
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
            updated_at TEXT,
            FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bt_runs_code ON backtest_runs(code)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bt_runs_strategy ON backtest_runs(strategy_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC)')

        # -----------------------------------------------------------
        # 4. 回测结果（统计指标）
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_results (
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
            equity_curve_json TEXT,
            drawdown_curve_json TEXT,
            monthly_returns_json TEXT,
            engine_version TEXT DEFAULT 'v1.0',
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
        )''')

        # -----------------------------------------------------------
        # 5. 交易记录
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_trades (
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
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id, trade_no)')

        # -----------------------------------------------------------
        # 6. 批量回测批次主表（新增）
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_batch (
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
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_batch_status ON backtest_batch(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_batch_created ON backtest_batch(created_at DESC)')

        # -----------------------------------------------------------
        # 7. 批量回测明细表（新增）
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_batch_item (
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
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_item_batch ON backtest_batch_item(batch_id)')

        # -----------------------------------------------------------
        # 8. 常用测试集（新增）
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_test_set (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            codes_json TEXT NOT NULL,
            tags_json TEXT,
            code_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_test_set_name ON backtest_test_set(name)')

        # -----------------------------------------------------------
        # 8.5 多策略批量回测聚合表
        # -----------------------------------------------------------
        c.execute('''CREATE TABLE IF NOT EXISTS backtest_multi (
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
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_multi_status ON backtest_multi(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_multi_created ON backtest_multi(created_at DESC)')

        c.execute('''CREATE TABLE IF NOT EXISTS backtest_multi_item (
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
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_multi_item_multi ON backtest_multi_item(multi_id)')

        # -----------------------------------------------------------
        # 9. 注册内置策略
        # -----------------------------------------------------------
        _register_builtin_strategies(c)


def _register_builtin_strategies(cursor):
    """注册内置策略（如不存在则插入）

    从 backtest/strategies/json_templates/ 读取 JSON 模板，
    写入 strategies.code 字段。params_schema 由 JSON 的 params 自动提取。

    注意：每次调用都会先删除所有 is_builtin=1 的记录，然后重新插入，
    确保数据库中只有最新的模板数据（避免旧模板残留）。
    """
    import os as _os
    from utils.app_paths import get_app_dir as _get_app_dir

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    template_dir = _os.path.join(_get_app_dir(), 'backtest', 'strategies', 'json_templates')

    if not _os.path.isdir(template_dir):
        return

    # 先删除所有内置策略（确保旧模板被清理）
    cursor.execute('DELETE FROM strategies WHERE is_builtin = 1')

    for fname in sorted(_os.listdir(template_dir)):
        if not fname.endswith('.json'):
            continue

        try:
            with open(_os.path.join(template_dir, fname), encoding='utf-8') as f:
                strategy_json = json.load(f)
        except Exception:
            continue

        meta = strategy_json.get('meta', {})
        raw_params = strategy_json.get('params', {})

        # 从 JSON params 自动生成 params_schema
        params_schema = []
        for key, info in raw_params.items():
            if isinstance(info, dict):
                params_schema.append({
                    'key': key,
                    'label': info.get('label', key),
                    'type': info.get('type', 'float'),
                    'default': info.get('value', info.get('default', 0)),
                    'min': info.get('min', 0),
                    'max': info.get('max', 999999),
                    'unit': info.get('unit', ''),
                })
            else:
                params_schema.append({
                    'key': key, 'label': key, 'type': 'float',
                    'default': info, 'min': 0, 'max': 999999, 'unit': '',
                })

        tags = json.dumps(meta.get('tags', []), ensure_ascii=False)
        strategy_id = meta.get('strategy_id', fname.replace('.json', ''))
        code_json = json.dumps(strategy_json, ensure_ascii=False)
        params_json = json.dumps(params_schema, ensure_ascii=False)

        cursor.execute('''INSERT INTO strategies
            (strategy_id, name, description, category, is_builtin, code, params_schema, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)''',
            (strategy_id,
             meta.get('name', fname),
             meta.get('description', ''),
             meta.get('category', 'custom'),
             code_json, params_json, tags, now, now))


# ============================================================
# data_tasks CRUD
# ============================================================

def create_data_task(task_id: str, task_type: str, config_json: str) -> Dict[str, Any]:
    """创建采集任务"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT INTO data_tasks (task_id, task_type, status, config_json, created_at, updated_at)
                        VALUES (?, ?, 'pending', ?, ?, ?)''',
                     (task_id, task_type, config_json, now, now))
    return get_data_task(task_id)


def get_data_task(task_id: str) -> Optional[Dict[str, Any]]:
    """获取单个采集任务"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM data_tasks WHERE task_id = ?', (task_id,)).fetchone()
        return dict(row) if row else None


def get_data_tasks(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """获取采集任务列表"""
    with get_backtest_db() as conn:
        if status:
            rows = conn.execute('SELECT * FROM data_tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?',
                                (status, limit)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM data_tasks ORDER BY created_at DESC LIMIT ?',
                                (limit,)).fetchall()
        return [dict(r) for r in rows]


def update_data_task(task_id: str, **kwargs) -> None:
    """更新采集任务字段"""
    if not kwargs:
        return
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE data_tasks SET {sets} WHERE task_id = ?', values)


def append_task_log(task_id: str, log_line: str) -> None:
    """追加采集日志"""
    now = datetime.now().strftime('%H:%M:%S')
    line = f'[{now}] {log_line}\n'
    with get_backtest_db() as conn:
        conn.execute('''UPDATE data_tasks SET log_text = COALESCE(log_text, '') || ?, updated_at = ?
                        WHERE task_id = ?''',
                     (line, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), task_id))


# ============================================================
# strategies CRUD
# ============================================================

def get_strategies(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取策略列表"""
    with get_backtest_db() as conn:
        if category:
            rows = conn.execute('SELECT * FROM strategies WHERE category = ? ORDER BY is_builtin DESC, name',
                                (category,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM strategies ORDER BY is_builtin DESC, name').fetchall()
        return [dict(r) for r in rows]


def get_strategy(strategy_id: str) -> Optional[Dict[str, Any]]:
    """获取单个策略"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM strategies WHERE strategy_id = ?', (strategy_id,)).fetchone()
        return dict(row) if row else None


def upsert_strategy(strategy_id: str, name: str, description: str, category: str,
                    params_schema: str, is_builtin: bool = False,
                    code: Optional[str] = None, tags: Optional[str] = None) -> None:
    """插入/更新策略"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT OR REPLACE INTO strategies
            (strategy_id, name, description, category, is_builtin, code, params_schema, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (strategy_id, name, description, category, 1 if is_builtin else 0,
                      code, params_schema, tags, now, now))


def delete_strategy(strategy_id: str) -> bool:
    """删除用户策略（内置策略不可删除）

    Args:
        strategy_id: 策略 ID

    Returns:
        True 如果删除了记录，False 如果策略不存在或是内置策略
    """
    with get_backtest_db() as conn:
        # 先检查是否是内置策略
        row = conn.execute(
            'SELECT is_builtin FROM strategies WHERE strategy_id = ?', (strategy_id,)
        ).fetchone()
        if not row:
            return False
        if row['is_builtin']:
            raise ValueError(f'内置策略不可删除: {strategy_id}')
        conn.execute('DELETE FROM strategies WHERE strategy_id = ?', (strategy_id,))
        return True


def _resolve_stock_name(code: str) -> str:
    """从 market_data.stock_info 查标的名称，查不到返回空字符串"""
    try:
        from utils.cache.market_data_db import get_stock_info
        info = get_stock_info(code)
        if info and info.get('name'):
            return info['name']
    except Exception:
        pass
    return ''


# ============================================================
# backtest_runs CRUD
# ============================================================

def create_backtest_run(run_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """创建回测任务"""
    # 补全标的名称：如果 config 里没传 stock_name，从 market_data 查
    stock_name = config.get('stock_name') or ''
    if not stock_name:
        stock_name = _resolve_stock_name(config['code'])
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT INTO backtest_runs
            (run_id, status, code, stock_name, strategy_id, strategy_name, params_json, config_json,
             start_date, end_date, initial_cash, commission, min_commission, stamp_tax, transfer_fee, slippage,
             t_plus_1, lot_size, adjust_type, stop_loss, take_profit, trailing_stop,
             position_mode, position_size, created_at, updated_at)
            VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (run_id, config['code'], stock_name, config['strategy_id'],
             config.get('strategy_name', ''), json.dumps(config.get('params', {}), ensure_ascii=False),
             json.dumps(config, ensure_ascii=False), config['start_date'], config['end_date'],
             config.get('initial_cash', 100000), config.get('commission', 0.0003),
             config.get('min_commission', 5.0), config.get('stamp_tax', 0.0005),
             config.get('transfer_fee', 0.00001), config.get('slippage', 0.001),
             1 if config.get('t_plus_1', True) else 0, config.get('lot_size', 100),
             config.get('adjust_type', 'qfq'), config.get('stop_loss', 0),
             config.get('take_profit', 0), config.get('trailing_stop', 0),
             config.get('position_mode', 'full'), config.get('position_size', 1.0),
             now, now))
    return get_backtest_run(run_id)


def get_backtest_run(run_id: str) -> Optional[Dict[str, Any]]:
    """获取单个回测任务"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_runs WHERE run_id = ?', (run_id,)).fetchone()
        return dict(row) if row else None


def get_backtest_runs(code: Optional[str] = None, strategy_id: Optional[str] = None,
                      limit: int = 50, offset: int = 0) -> tuple:
    """获取回测任务列表（含关键结果指标）+ 总数。

    返回 (rows, total)，支持分页。
    """
    conditions, params = [], []
    if code:
        conditions.append('r.code = ?')
        params.append(code)
    if strategy_id:
        conditions.append('r.strategy_id = ?')
        params.append(strategy_id)
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    with get_backtest_db() as conn:
        total = conn.execute(
            f'SELECT COUNT(*) FROM backtest_runs r {where}', params
        ).fetchone()[0]
        rows = conn.execute(f'''
            SELECT r.*,
                   res.total_return, res.annual_return, res.sharpe_ratio,
                   res.max_drawdown, res.trade_count, res.win_rate,
                   res.final_equity, res.calmar_ratio
            FROM backtest_runs r
            LEFT JOIN backtest_results res ON r.run_id = res.run_id
            {where}
            ORDER BY r.created_at DESC LIMIT ? OFFSET ?
        ''', params + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total


def update_backtest_run(run_id: str, **kwargs) -> None:
    """更新回测任务"""
    if not kwargs:
        return
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [run_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE backtest_runs SET {sets} WHERE run_id = ?', values)


def delete_backtest_run(run_id: str) -> bool:
    """删除回测任务（级联删除 results + trades）"""
    with get_backtest_db() as conn:
        conn.execute('DELETE FROM backtest_trades WHERE run_id = ?', (run_id,))
        conn.execute('DELETE FROM backtest_results WHERE run_id = ?', (run_id,))
        cur = conn.execute('DELETE FROM backtest_runs WHERE run_id = ?', (run_id,))
        return cur.rowcount > 0


# ============================================================
# backtest_results CRUD
# ============================================================

def save_backtest_result(run_id: str, result: Dict[str, Any]) -> None:
    """保存回测结果"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT OR REPLACE INTO backtest_results
            (run_id, total_return, annual_return, benchmark_return, excess_return,
             sharpe_ratio, sortino_ratio, calmar_ratio, max_drawdown, max_drawdown_duration,
             volatility, downside_volatility, trade_count, win_count, loss_count, win_rate,
             profit_factor, avg_win, avg_loss, avg_holding_days, max_consecutive_wins,
             max_consecutive_losses, final_equity, peak_equity,
             equity_curve_json, drawdown_curve_json, monthly_returns_json, engine_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (run_id, result.get('total_return'), result.get('annual_return'),
             result.get('benchmark_return'), result.get('excess_return'),
             result.get('sharpe_ratio'), result.get('sortino_ratio'), result.get('calmar_ratio'),
             result.get('max_drawdown'), result.get('max_drawdown_duration'),
             result.get('volatility'), result.get('downside_volatility'),
             result.get('trade_count'), result.get('win_count'), result.get('loss_count'),
             result.get('win_rate'), result.get('profit_factor'),
             result.get('avg_win'), result.get('avg_loss'), result.get('avg_holding_days'),
             result.get('max_consecutive_wins'), result.get('max_consecutive_losses'),
             result.get('final_equity'), result.get('peak_equity'),
             result.get('equity_curve_json'), result.get('drawdown_curve_json'),
             result.get('monthly_returns_json'), result.get('engine_version', 'v1.0'), now))


def get_backtest_result(run_id: str) -> Optional[Dict[str, Any]]:
    """获取回测结果"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_results WHERE run_id = ?', (run_id,)).fetchone()
        return dict(row) if row else None


# ============================================================
# backtest_trades CRUD
# ============================================================

def save_backtest_trades(run_id: str, trades: List[Dict[str, Any]]) -> None:
    """批量保存交易记录"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        for t in trades:
            conn.execute('''INSERT INTO backtest_trades
                (run_id, trade_no, direction, trade_date, price, quantity, amount,
                 commission, stamp_tax, transfer_fee, slippage_cost, total_cost,
                 pnl, pnl_pct, holding_days, signal_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (run_id, t.get('trade_no'), t.get('direction'), t.get('trade_date'),
                 t.get('price'), t.get('quantity'), t.get('amount'),
                 t.get('commission', 0), t.get('stamp_tax', 0), t.get('transfer_fee', 0),
                 t.get('slippage_cost', 0), t.get('total_cost', 0),
                 t.get('pnl'), t.get('pnl_pct'), t.get('holding_days'),
                 t.get('signal_reason'), now))


def get_backtest_trades(run_id: str) -> List[Dict[str, Any]]:
    """获取交易记录"""
    with get_backtest_db() as conn:
        rows = conn.execute('SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY trade_no',
                            (run_id,)).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 数据统计
# ============================================================

def get_data_stats() -> Dict[str, Any]:
    """获取数据库统计信息"""
    with get_backtest_db() as conn:
        stats = {}
        for table in ['data_tasks', 'strategies', 'backtest_runs', 'backtest_results', 'backtest_trades',
                       'backtest_batch', 'backtest_batch_item', 'backtest_test_set',
                       'backtest_multi', 'backtest_multi_item']:
            row = conn.execute(f'SELECT COUNT(*) as cnt FROM {table}').fetchone()
            stats[table] = row['cnt'] if row else 0
        return stats


# ============================================================
# backtest_batch CRUD（批量回测批次主表）
# ============================================================

def create_batch(batch_id: str, strategy_id: str, strategy_name: str,
                 params: Dict[str, Any], config: Dict[str, Any], codes: List[str],
                 initial_cash: float, start_date: str, end_date: str) -> None:
    """创建批次主记录（status=pending）"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT INTO backtest_batch
            (batch_id, strategy_id, strategy_name, params_json, config_json, codes_json,
             code_count, status, initial_cash, start_date, end_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)''',
            (batch_id, strategy_id, strategy_name,
             json.dumps(params, ensure_ascii=False),
             json.dumps(config, ensure_ascii=False),
             json.dumps(codes, ensure_ascii=False),
             len(codes), initial_cash, start_date, end_date, now))


def get_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    """获取批次主记录"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_batch WHERE batch_id = ?',
                           (batch_id,)).fetchone()
        return dict(row) if row else None


def list_batches(limit: int = 20, offset: int = 0) -> tuple:
    """获取批次列表（按创建时间倒序）+ 总数。返回 (rows, total)。"""
    with get_backtest_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) FROM backtest_batch'
        ).fetchone()[0]
        rows = conn.execute(
            'SELECT * FROM backtest_batch ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (limit, offset)).fetchall()
        return [dict(r) for r in rows], total


def update_batch(batch_id: str, **kwargs) -> None:
    """更新批次字段"""
    if not kwargs:
        return
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [batch_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE backtest_batch SET {sets} WHERE batch_id = ?', values)


def delete_batch(batch_id: str) -> None:
    """删除批次（连同明细）"""
    with get_backtest_db() as conn:
        conn.execute('DELETE FROM backtest_batch_item WHERE batch_id = ?', (batch_id,))
        conn.execute('DELETE FROM backtest_batch WHERE batch_id = ?', (batch_id,))


# ============================================================
# backtest_batch_item CRUD（批次明细）
# ============================================================

def create_batch_items(batch_id: str, items: List[Dict[str, Any]]) -> None:
    """批量创建明细（status=pending）

    Args:
        items: [{code, asset_type, run_id}, ...]
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.executemany('''INSERT INTO backtest_batch_item
            (batch_id, code, asset_type, run_id, status, started_at)
            VALUES (?, ?, ?, ?, 'pending', ?)''',
            [(batch_id, it['code'], it.get('asset_type'), it.get('run_id'), now)
             for it in items])


def get_batch_items(batch_id: str) -> List[Dict[str, Any]]:
    """获取批次所有明细（按 id 升序，保持提交顺序）"""
    with get_backtest_db() as conn:
        rows = conn.execute(
            'SELECT * FROM backtest_batch_item WHERE batch_id = ? ORDER BY id',
            (batch_id,)).fetchall()
        return [dict(r) for r in rows]


def update_batch_item(batch_id: str, code: str, **kwargs) -> None:
    """更新单条明细"""
    if not kwargs:
        return
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [batch_id, code]
    with get_backtest_db() as conn:
        conn.execute(
            f'UPDATE backtest_batch_item SET {sets} WHERE batch_id = ? AND code = ?',
            values)


def get_batch_item_by_code(batch_id: str, code: str) -> Optional[Dict[str, Any]]:
    """按 code 获取单条明细"""
    with get_backtest_db() as conn:
        row = conn.execute(
            'SELECT * FROM backtest_batch_item WHERE batch_id = ? AND code = ?',
            (batch_id, code)).fetchone()
        return dict(row) if row else None


# ============================================================
# backtest_test_set CRUD（常用测试集）
# ============================================================

def create_test_set(name: str, codes: List[str],
                    description: str = '', tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """创建测试集

    Returns:
        新建的测试集记录
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT INTO backtest_test_set
            (name, description, codes_json, tags_json, code_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (name, description,
             json.dumps(codes, ensure_ascii=False),
             json.dumps(tags or [], ensure_ascii=False),
             len(codes), now, now))
    return get_test_set_by_name(name)


def get_test_set_by_name(name: str) -> Optional[Dict[str, Any]]:
    """按 name 获取测试集"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_test_set WHERE name = ?',
                           (name,)).fetchone()
        return dict(row) if row else None


def get_test_set(test_set_id: int) -> Optional[Dict[str, Any]]:
    """按 id 获取测试集"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_test_set WHERE id = ?',
                           (test_set_id,)).fetchone()
        return dict(row) if row else None


def list_test_sets() -> List[Dict[str, Any]]:
    """获取全部测试集（按更新时间倒序）"""
    with get_backtest_db() as conn:
        rows = conn.execute(
            'SELECT * FROM backtest_test_set ORDER BY updated_at DESC').fetchall()
        return [dict(r) for r in rows]


def update_test_set(test_set_id: int, **kwargs) -> None:
    """更新测试集字段

    支持快捷参数:
        - codes: list[str] → 自动序列化为 codes_json 并更新 code_count
        - tags:  list[str] → 自动序列化为 tags_json
    """
    if not kwargs:
        return
    if 'codes' in kwargs:
        codes = kwargs.pop('codes')
        kwargs['codes_json'] = json.dumps(codes, ensure_ascii=False)
        kwargs['code_count'] = len(codes)
    if 'tags' in kwargs:
        kwargs['tags_json'] = json.dumps(kwargs.pop('tags'), ensure_ascii=False)
    kwargs['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [test_set_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE backtest_test_set SET {sets} WHERE id = ?', values)


def delete_test_set(test_set_id: int) -> bool:
    """删除测试集

    Returns:
        True 如果删除了记录，False 如果测试集不存在
    """
    with get_backtest_db() as conn:
        cur = conn.execute('DELETE FROM backtest_test_set WHERE id = ?', (test_set_id,))
        return cur.rowcount > 0


# ============================================================
# backtest_multi CRUD（多策略批量回测聚合）
# ============================================================

def create_multi(multi_id: str, strategy_ids: List[str], strategy_names: List[str],
                 codes: List[str], initial_cash: float, start_date: str,
                 end_date: str) -> None:
    """创建多策略批次主记录"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('''INSERT INTO backtest_multi
            (multi_id, strategy_ids_json, strategy_names_json, codes_json,
             code_count, strategy_count, status, initial_cash, start_date, end_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)''',
            (multi_id, json.dumps(strategy_ids, ensure_ascii=False),
             json.dumps(strategy_names, ensure_ascii=False),
             json.dumps(codes, ensure_ascii=False),
             len(codes), len(strategy_ids),
             initial_cash, start_date, end_date, now))


def get_multi(multi_id: str) -> Optional[Dict[str, Any]]:
    """获取多策略批次主记录"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_multi WHERE multi_id = ?',
                           (multi_id,)).fetchone()
        return dict(row) if row else None


def list_multi(limit: int = 20, offset: int = 0) -> tuple:
    """获取多策略批次列表（按创建时间倒序）+ 总数。返回 (rows, total)。"""
    with get_backtest_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) FROM backtest_multi'
        ).fetchone()[0]
        rows = conn.execute(
            'SELECT * FROM backtest_multi ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (limit, offset)).fetchall()
        return [dict(r) for r in rows], total


def update_multi(multi_id: str, **kwargs) -> None:
    """更新多策略批次字段"""
    if not kwargs:
        return
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [multi_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE backtest_multi SET {sets} WHERE multi_id = ?', values)


def delete_multi(multi_id: str) -> None:
    """删除多策略批次（级联删除 items 和关联 batches）"""
    with get_backtest_db() as conn:
        # 级联删除所有关联的 batch（含 batch_items）
        rows = conn.execute(
            'SELECT DISTINCT batch_id FROM backtest_multi_item WHERE multi_id = ? AND batch_id IS NOT NULL',
            (multi_id,)).fetchall()
        for r in rows:
            conn.execute('DELETE FROM backtest_batch_item WHERE batch_id = ?', (r['batch_id'],))
            conn.execute('DELETE FROM backtest_batch WHERE batch_id = ?', (r['batch_id'],))
        conn.execute('DELETE FROM backtest_multi_item WHERE multi_id = ?', (multi_id,))
        conn.execute('DELETE FROM backtest_multi WHERE multi_id = ?', (multi_id,))


def create_multi_items(multi_id: str, items: List[Dict[str, Any]]) -> None:
    """批量创建 multi items（M×N 条）

    items: [{strategy_id, code, batch_id, run_id}, ...]
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.executemany('''INSERT OR IGNORE INTO backtest_multi_item
            (multi_id, strategy_id, code, batch_id, run_id, status)
            VALUES (?, ?, ?, ?, ?, 'pending')''',
            [(multi_id, it['strategy_id'], it['code'],
              it.get('batch_id'), it.get('run_id'))
             for it in items])


def get_multi_items(multi_id: str) -> List[Dict[str, Any]]:
    """获取多策略批次所有明细"""
    with get_backtest_db() as conn:
        rows = conn.execute(
            'SELECT * FROM backtest_multi_item WHERE multi_id = ? ORDER BY strategy_id, code',
            (multi_id,)).fetchall()
        return [dict(r) for r in rows]


def update_multi_item(multi_id: str, strategy_id: str, code: str, **kwargs) -> None:
    """更新单条 multi item"""
    if not kwargs:
        return
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [multi_id, strategy_id, code]
    with get_backtest_db() as conn:
        conn.execute(
            f'UPDATE backtest_multi_item SET {sets} WHERE multi_id = ? AND strategy_id = ? AND code = ?',
            values)


def mark_interrupted_runs() -> Dict[str, int]:
    """服务重启时把所有 running/pending 的回测标记为 failed(中断)。

    覆盖三张主表（backtest_runs / backtest_batch / backtest_multi）及关联明细表，
    避免 SSE 永远显示 running。返回各表清理条数。
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg = '服务重启中断'
    counts: Dict[str, int] = {}
    with get_backtest_db() as conn:
        counts['runs'] = conn.execute(
            "UPDATE backtest_runs SET status='failed', error_message=?, "
            "completed_at=? WHERE status IN ('running','pending')",
            (msg, now)
        ).rowcount
        counts['batches'] = conn.execute(
            "UPDATE backtest_batch SET status='failed', error_message=? "
            "WHERE status IN ('running','pending')", (msg,)
        ).rowcount
        counts['batch_items'] = conn.execute(
            "UPDATE backtest_batch_item SET status='failed', "
            "error_message=?, completed_at=? WHERE status IN ('running','pending')",
            (msg, now)
        ).rowcount
        counts['multis'] = conn.execute(
            "UPDATE backtest_multi SET status='failed', error_message=? "
            "WHERE status IN ('running','pending')", (msg,)
        ).rowcount
        counts['multi_items'] = conn.execute(
            "UPDATE backtest_multi_item SET status='failed', completed_at=? "
            "WHERE status IN ('running','pending')", (now,)
        ).rowcount
    return counts
