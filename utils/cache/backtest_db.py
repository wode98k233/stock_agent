"""backtest.db 连接管理和表初始化

职责：
- data_tasks: 数据采集任务管理
- strategies: 策略定义（内置+用户自定义）
- backtest_runs: 回测任务
- backtest_results: 回测结果（统计指标+曲线数据）
- backtest_trades: 交易记录
"""
import json
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from config import Config
from utils.cache.db_utils import make_db_context

get_backtest_db = make_db_context(lambda: Config.get_backtest_db_path())

# ---- 列存在性探测（迁移未应用时的降级兜底）--------------------------------
# B 批给 backtest_runs / strategy_session 加了 ``exit_timing`` 列（加性迁移，
# 事实源在 utils/db_schema/tables/backtest.py）。生产库若还没跑迁移，INSERT 里
# 带这个列会直接 "table has no column named exit_timing" 把整条策略回测链打断。
#
# 这里在**进程内**缓存一次探测结果：迁移补上后重启进程即可恢复（缓存不跨进程）。
# 降级行为是「这一列不写」，而不是抛错 —— 宁可少存一个可选字段，也不能让整条链路挂掉。
_MISSING_COLS: Dict[str, set] = {}


def _missing_columns(conn, table: str, columns) -> set:
    """返回 ``columns`` 中在当前库里**不存在**的那些（按表缓存，首次查一次）。"""
    cached = _MISSING_COLS.get(table)
    if cached is None:
        cached = _MISSING_COLS[table] = set()
    todo = [c for c in columns if c not in cached]
    if todo:
        try:
            have = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}
        except Exception:
            have = set()
        cached.update(c for c in todo if c not in have)
    return {c for c in columns if c in cached}


def init_backtest_tables():
    """初始化 backtest.db 表结构（DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    with get_backtest_db() as conn:
        # 启用 WAL 模式：允许并发读写，避免批量回测多线程写 DB 时锁冲突
        conn.execute("PRAGMA journal_mode = WAL")
        d = ALL_DBS["backtest"]
        ensure_schema(conn, d.tables, d.special_migrations)
        for seed in d.seeds:
            seed(conn)


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


def set_data_task_origin(task_id: str, origin: str, ref_id: str = '') -> None:
    """标记任务来源（manual / backtest_gap）+ 反指触发的回测 run_id。"""
    with get_backtest_db() as conn:
        conn.execute('UPDATE data_tasks SET origin = ?, ref_id = ? WHERE task_id = ?',
                     (origin or 'manual', ref_id or '', task_id))


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


def update_strategy_hash(strategy_id: str, strategy_hash_value: str) -> bool:
    """回写策略指纹（设计 §8 修 B5：采纳后的策略要能反查研发迭代史）。"""
    with get_backtest_db() as conn:
        cur = conn.execute('UPDATE strategies SET strategy_hash = ?, updated_at = ? WHERE strategy_id = ?',
                           (strategy_hash_value or '',
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S'), strategy_id))
        return cur.rowcount > 0


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

def _cfg_default_limit_mode() -> str:
    """全局默认涨跌停拦截口径（P0 §4.7），读不到时回落 enforce。"""
    try:
        from config import Config
        return str(getattr(Config, 'PRICE_LIMIT_MODE', 'enforce') or 'enforce')
    except Exception:  # noqa: BLE001
        return 'enforce'


def create_backtest_run(run_id: str, config: Dict[str, Any],
                       origin: str = 'manual', ref_id: str = '') -> Dict[str, Any]:
    """创建回测任务"""
    # 补全标的名称：如果 config 里没传 stock_name，从 market_data 查
    stock_name = config.get('stock_name') or ''
    if not stock_name:
        stock_name = _resolve_stock_name(config['code'])
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # P0 §4.7：口径快照——记录本次回测实际使用的拦截模式（单次请求 > 全局配置）
    from backtest import a_share_rules
    limit_mode = a_share_rules.resolve_mode(config.get('price_limit_mode'),
                                            _cfg_default_limit_mode())
    with get_backtest_db() as conn:
        # P0 §4.7：列与值改为**列表构造**——新增可选列只加一行，不再新增 if/else 分支。
        base = [
            ('run_id', run_id), ('status', 'pending'), ('code', config['code']),
            ('stock_name', stock_name), ('strategy_id', config['strategy_id']),
            ('strategy_name', config.get('strategy_name', '')),
            ('params_json', json.dumps(config.get('params', {}), ensure_ascii=False)),
            ('config_json', json.dumps(config, ensure_ascii=False)),
            ('start_date', config['start_date']), ('end_date', config['end_date']),
            ('initial_cash', config.get('initial_cash', 100000)),
            ('commission', config.get('commission', 0.0003)),
            ('min_commission', config.get('min_commission', 5.0)),
            ('stamp_tax', config.get('stamp_tax', 0.0005)),
            ('transfer_fee', config.get('transfer_fee', 0.00001)),
            ('slippage', config.get('slippage', 0.001)),
            ('t_plus_1', 1 if config.get('t_plus_1', True) else 0),
            ('lot_size', config.get('lot_size', 100)),
            ('adjust_type', config.get('adjust_type', 'qfq')),
            ('stop_loss', config.get('stop_loss', 0)),
            ('take_profit', config.get('take_profit', 0)),
            ('trailing_stop', config.get('trailing_stop', 0)),
            ('position_mode', config.get('position_mode', 'full')),
            ('position_size', config.get('position_size', 1.0)),
            ('created_at', now), ('updated_at', now), ('origin', origin), ('ref_id', ref_id),
        ]
        # 可选列：迁移未应用时自动降级为不写（_missing_columns 按表缓存探测）。
        # engine_version：enforce 口径的 run 记 v1.1，作为「无新列时」的二级可辨识信号。
        optional = [
            ('entry_timing', str(config.get('entry_timing') or 'next_open')),
            ('exit_timing', str(config.get('exit_timing') or '')),
            ('price_limit_mode', limit_mode),
            ('engine_version', 'v1.1' if limit_mode == 'enforce' else 'v1.0'),
        ]
        skip = _missing_columns(conn, 'backtest_runs', tuple(k for k, _ in optional))
        cols = base + [(k, v) for k, v in optional if k not in skip]
        names = [k for k, _ in cols]
        conn.execute(
            f"INSERT INTO backtest_runs ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [v for _, v in cols])
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
    """保存回测结果。

    P1：列与值改**列表构造 + ``_missing_columns`` 降级**——新增可选列（sr_daily /
    skewness / kurtosis / yearly_stats_json / deflated_sharpe / trial_count）在
    迁移未应用时自动跳过，INSERT 不炸。
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        cols = [
            ('run_id', run_id),
            ('total_return', result.get('total_return')),
            ('annual_return', result.get('annual_return')),
            ('benchmark_return', result.get('benchmark_return')),
            ('excess_return', result.get('excess_return')),
            ('sharpe_ratio', result.get('sharpe_ratio')),
            ('sortino_ratio', result.get('sortino_ratio')),
            ('calmar_ratio', result.get('calmar_ratio')),
            ('max_drawdown', result.get('max_drawdown')),
            ('max_drawdown_duration', result.get('max_drawdown_duration')),
            ('volatility', result.get('volatility')),
            ('downside_volatility', result.get('downside_volatility')),
            ('trade_count', result.get('trade_count')),
            ('win_count', result.get('win_count')),
            ('loss_count', result.get('loss_count')),
            ('win_rate', result.get('win_rate')),
            ('profit_factor', result.get('profit_factor')),
            ('avg_win', result.get('avg_win')),
            ('avg_loss', result.get('avg_loss')),
            ('avg_holding_days', result.get('avg_holding_days')),
            ('max_consecutive_wins', result.get('max_consecutive_wins')),
            ('max_consecutive_losses', result.get('max_consecutive_losses')),
            ('final_equity', result.get('final_equity')),
            ('peak_equity', result.get('peak_equity')),
            ('equity_curve_json', result.get('equity_curve_json')),
            ('drawdown_curve_json', result.get('drawdown_curve_json')),
            ('monthly_returns_json', result.get('monthly_returns_json')),
            ('yearly_stats_json', result.get('yearly_stats_json')),
            ('sr_daily', result.get('sr_daily')),
            ('skewness', result.get('skewness')),
            ('kurtosis', result.get('kurtosis')),
            ('deflated_sharpe', result.get('deflated_sharpe')),
            ('trial_count', result.get('trial_count')),
            ('health', result.get('health', 'ok')),
            ('health_detail', result.get('health_detail', '')),
            ('data_bars', result.get('data_bars', 0)),
            ('expected_bars', result.get('expected_bars', 0)),
            ('coverage_pct', result.get('coverage_pct', 0.0)),
            ('gaps_json', result.get('gaps_json', '[]')),
            ('trade_sample_ok', result.get('trade_sample_ok', 0)),
            ('engine_version', result.get('engine_version', 'v1.0')),
            ('created_at', now),
        ]
        skip = _missing_columns(conn, 'backtest_results', tuple(k for k, _ in cols))
        final = [(k, v) for k, v in cols if k not in skip]
        names = [k for k, _ in final]
        conn.execute(
            f"INSERT OR REPLACE INTO backtest_results ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [v for _, v in final])


def get_backtest_result(run_id: str) -> Optional[Dict[str, Any]]:
    """获取回测结果"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_results WHERE run_id = ?', (run_id,)).fetchone()
        return dict(row) if row else None


# ============================================================
# trial 记账 / DSR 回填（P1 统计严谨 §4）
# ============================================================

def count_trials_for_strategy(strategy_hash: str) -> int:
    """同策略所在研发任务的迭代轮数（P1 §4.1，实测 8 轮制口径）。

    ``strategy_iter`` 每轮一行、不覆盖（策略 hash 级实测恒 1 行——每轮变异产生新
    hash——故 trial 口径取「**该 hash 所属研发任务的全部轮数**」）。
    无记录（散点评估）→ 1。
    """
    if not strategy_hash:
        return 1
    with get_backtest_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategy_iter WHERE run_id IN "
            "(SELECT DISTINCT run_id FROM strategy_iter WHERE strategy_hash = ?)",
            (strategy_hash,)).fetchone()
    return int(row[0]) if row and row[0] else 1


def trial_sharpe_std(strategy_hash: str) -> tuple:
    """同任务内 trial 夏普序列的 stdev（**年化**口径）+ 来源（P1 §4.1b 三级降级）。

    Returns ``(sr_std, source)``：

    - ``'sharpe'``：新口径（≥2 个非空值）
    - ``'sharpe_legacy'``：旧口径兜底（新值不足时，口径已标注不可比——仅用于
      sr_std 的**组内**估计）
    - ``'none'``：都不足 → 不校正、不造数
    """
    if not strategy_hash:
        return None, 'none'
    import statistics as _st
    with get_backtest_db() as conn:
        rows = conn.execute(
            "SELECT is_metrics FROM strategy_iter WHERE run_id IN "
            "(SELECT DISTINCT run_id FROM strategy_iter WHERE strategy_hash = ?) "
            "ORDER BY round", (strategy_hash,)).fetchall()

    def _series(key: str) -> list:
        out = []
        for (m,) in rows:
            try:
                d = json.loads(m or '{}')
            except Exception:
                continue
            v = d.get(key)
            if v is not None:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    pass
        return out

    new = _series('sharpe')
    if len(new) >= 2:
        return _st.stdev(new), 'sharpe'
    legacy = _series('sharpe_legacy')
    if len(legacy) >= 2:
        return _st.stdev(legacy), 'sharpe_legacy'
    return None, 'none'


def update_result_dsr(run_id: str, deflated_sharpe, trial_count) -> None:
    """回填结果的 DSR / trial 列（P1 §4；迁移未应用时静默跳过）。"""
    with get_backtest_db() as conn:
        skip = _missing_columns(conn, 'backtest_results', ('deflated_sharpe', 'trial_count'))
        if skip:
            return
        conn.execute(
            'UPDATE backtest_results SET deflated_sharpe = ?, trial_count = ? WHERE run_id = ?',
            (deflated_sharpe, trial_count, run_id))


# ============================================================
# backtest_eval CRUD（样本内外评判，Plan 3 任务 2）
# ============================================================

_BACKTEST_EVAL_UPDATABLE = {
    'is_metrics_json', 'oos_metrics_json', 'split_date',
    'is_sharpe', 'oos_sharpe', 'sharpe_ratio_io',
    'overfit_flag', 'verdict', 'eval_run_ids_json', 'error_message',
}


def save_backtest_eval(run_id: str, data: Dict[str, Any]) -> None:
    """写入/覆盖一次样本内外评判（单表单行，重复评判以最后一次为准）。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    payload = {k: v for k, v in (data or {}).items() if k in _BACKTEST_EVAL_UPDATABLE}
    payload['run_id'] = run_id
    payload['created_at'] = now
    payload['updated_at'] = now
    cols = ', '.join(payload.keys())
    marks = ', '.join('?' for _ in payload)
    with get_backtest_db() as conn:
        conn.execute(f'INSERT OR REPLACE INTO backtest_eval ({cols}) VALUES ({marks})',
                     tuple(payload.values()))


def get_backtest_eval(run_id: str) -> Optional[Dict[str, Any]]:
    """读取评判结果；没评判过返回 None（调用方据此显示「尚未评判」）。"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM backtest_eval WHERE run_id = ?', (run_id,)).fetchone()
    return dict(row) if row else None


def get_result_health_map(run_ids: List[str]) -> Dict[str, str]:
    """批量读取 run 的体检结论：run_id -> health。

    批量验证（Plan 3 任务 8）用它区分三种「有结果」：
    - health ∈ {ok, low_sample} → 结果可用于评判（达标 / 没达标）
    - 其余（data_gap / insufficient_bars / error ...）→ 无法评判，不能算「弱」

    旧库未迁移（没有 health 列 / 没有 backtest_results 表）时返回空表，
    调用方按「无法评判」处理，不因迁移状态炸掉整个统计。
    """
    ids = [rid for rid in (run_ids or []) if rid]
    if not ids:
        return {}
    placeholders = ','.join('?' * len(ids))
    try:
        with get_backtest_db() as conn:
            rows = conn.execute(
                f'SELECT run_id, health FROM backtest_results WHERE run_id IN ({placeholders})',
                ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r['run_id']: (r['health'] or '') for r in rows}


def get_result_meta_map(run_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量取体检元数据（health/health_detail/coverage_pct/trade_sample_ok/data_bars/expected_bars）。

    刻意不返回 equity_curve_json / drawdown_curve_json 这类大字段：策略详情页一次要看几十条 run。
    Plan 1 建列之前的历史行不会命中这些列 → 缺列时返回空字典，不抛异常。
    """
    ids = [r for r in (run_ids or []) if r]
    if not ids:
        return {}
    placeholders = ','.join('?' * len(ids))
    try:
        with get_backtest_db() as conn:
            rows = conn.execute(
                'SELECT run_id, health, health_detail, coverage_pct, trade_sample_ok, '
                'data_bars, expected_bars FROM backtest_results '
                f'WHERE run_id IN ({placeholders})',
                ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r['run_id']: dict(r) for r in rows}


# ============================================================
# 回测数据就绪度（设计文档 §12，Plan 4 任务 2 步骤 2.4）
# ============================================================

def get_backtest_readiness(days: int = 30) -> Dict[str, Any]:
    """近 N 天回测数据就绪度（设计文档 §12）。只读，绝不触发补数或网络请求。"""
    from datetime import timedelta
    span = max(1, int(days))
    since = (datetime.now() - timedelta(days=span)).strftime('%Y-%m-%d %H:%M:%S')
    out: Dict[str, Any] = {'days': span, 'runs': 0, 'avg_coverage_pct': None, 'gap_runs': 0,
                           'gap_tasks': 0, 'gap_tasks_done': 0, 'refilled': 0, 'hint': ''}
    try:
        with get_backtest_db() as conn:
            out['runs'] = conn.execute(
                'SELECT COUNT(*) FROM backtest_runs WHERE created_at >= ?', (since,)).fetchone()[0]
            row = conn.execute(
                'SELECT AVG(res.coverage_pct) AS avg_cov, '
                "SUM(CASE WHEN res.health = 'data_gap' THEN 1 ELSE 0 END) AS gap_runs "
                'FROM backtest_runs r JOIN backtest_results res ON r.run_id = res.run_id '
                'WHERE r.created_at >= ?', (since,)).fetchone()
            if row:
                # 口径：coverage_pct 落库为 0–1 比值（backtest/health.py），对外统一百分数
                out['avg_coverage_pct'] = (round(row['avg_cov'] * 100, 1)
                                           if row['avg_cov'] is not None else None)
                out['gap_runs'] = int(row['gap_runs'] or 0)
            out['gap_tasks'] = conn.execute(
                "SELECT COUNT(*) FROM data_tasks WHERE origin = 'backtest_gap' AND created_at >= ?",
                (since,)).fetchone()[0]
            out['gap_tasks_done'] = conn.execute(
                "SELECT COUNT(*) FROM data_tasks WHERE origin = 'backtest_gap' "
                "AND status = 'completed' AND created_at >= ?", (since,)).fetchone()[0]
            # 近似口径（计划偏差 2）：补过数、且之后重跑过，即算「补数后已重跑」
            out['refilled'] = conn.execute(
                'SELECT COUNT(DISTINCT t.ref_id) FROM data_tasks t '
                'JOIN backtest_runs r ON r.ref_id = t.ref_id '
                "WHERE t.origin = 'backtest_gap' AND t.status = 'completed' "
                "AND t.created_at >= ? "
                "AND r.origin = 'manual' AND r.created_at >= t.created_at",
                (since,)).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    cov = out['avg_coverage_pct']
    out['hint'] = (f"近 {span} 天回测 {out['runs']} 次，平均数据覆盖率 "
                   f"{'—' if cov is None else str(cov) + '%'}，"
                   f"其中 {out['gap_tasks']} 次因缺口补过数"
                   f"（补数后 {out['refilled']} 次已重跑）")
    return out


# ============================================================
# backtest_trades CRUD
# ============================================================

def save_backtest_trades(run_id: str, trades: List[Dict[str, Any]]) -> None:
    """批量保存交易记录。

    ⚠️ 必须先删后插（2026-11-07 修）：同一个 run 重跑时，旧实现只 INSERT，
    成交记录会一批批叠加——实测某 run `trade_count=29`，库里却有 55 买 55 卖
    （26 笔旧批 + 29 笔新批），全库 4 万多个重复键。后果是按成交重算的任何指标
    （胜率、滑点、费用）都会算重。
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        conn.execute('DELETE FROM backtest_trades WHERE run_id = ?', (run_id,))
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
                       'backtest_multi', 'backtest_multi_item',
                       'strategy_run', 'strategy_iter']:
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

    ⚠️ 这里**不碰** ``strategy_run``：策略研发的残留 running 由
    ``backtest.strategy_research.mark_interrupted_runs`` 收敛成 ``interrupted``
    （可续跑）。两边都写会互相覆盖 —— 谁先跑谁决定用户还能不能接着跑。
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


# ============================================================
# strategy_run / strategy_iter CRUD（A3 策略研发）
# ============================================================

_ITER_COLS = ('id, run_id, round, hypothesis, strategy_json, strategy_hash, '
              'strategy_diff, compile_ok, compile_error, is_metrics, oos_metrics, '
              'overfit_flag, verdict, model, created_at')


def create_strategy_run(run_uuid: str, idea: str, universe: str, period: str,
                        source: str = 'panel',
                        dialog_uuid: Optional[str] = None,
                        engine_version: str = '',
                        entry_timing: str = 'next_open',
                        exit_timing: str = '') -> Dict[str, Any]:
    """创建策略研究任务（status=running）。

    engine_version 标记指标口径（见 engine.py）。
    entry_timing / exit_timing：本次研究实际的成交口径，**必须落库**——只传给后台
    线程的话，续跑和结果卡只能回去猜会话，面板/技能发起的任务更是完全无据可查。
    exit_timing 空串 = 跟随 entry_timing（不是「没记录」）。
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        cols = [
            ('run_uuid', run_uuid), ('idea', idea), ('universe', universe),
            ('period', period), ('status', 'running'), ('source', source),
            ('dialog_uuid', dialog_uuid), ('created_at', now),
        ]
        # 可选列：迁移未跑时降级为**不写这一列**，而不是把整条研究链打断
        # （同 create_backtest_run / create_strategy_session，见
        #  test_backtest_db_migration_guard.py 的降级契约）。
        optional = [
            ('engine_version', engine_version),
            ('entry_timing', str(entry_timing or 'next_open')),
            ('exit_timing', str(exit_timing or '')),
        ]
        skip = _missing_columns(conn, 'strategy_run', tuple(k for k, _ in optional))
        cols += [(k, v) for k, v in optional if k not in skip]
        names = [k for k, _ in cols]
        conn.execute(
            f"INSERT INTO strategy_run ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [v for _, v in cols])
    return get_strategy_run(run_uuid)


def get_strategy_run(run_uuid: str) -> Optional[Dict[str, Any]]:
    """按 run_uuid 获取策略研究任务。

    ⚠️ 迁移未跑的旧库没有 entry_timing / exit_timing 列：这里**不补默认值**
    （数据缺失 ≠ 默认口径），由调用方归一化；补默认值会让「旧 run 没有口径记录」
    与「明确跑了 next_open」变得不可区分，续跑就没法回退到原会话。
    """
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM strategy_run WHERE run_uuid = ?', (run_uuid,)).fetchone()
        return dict(row) if row else None


# strategy_run 允许更新的列（白名单：kwargs 直接拼进 SQL，必须锁死）
_STRATEGY_RUN_UPDATABLE = {
    'status', 'stop_reason', 'best_iter_id', 'total_iters', 'completed_at',
    # C 批：策略研发自建 trace run 的归属（显式绑定，取代「猜最近 run」）
    'trace_run_id',
}


def update_strategy_run(run_uuid: str, **kwargs) -> None:
    """更新策略研究任务字段。

    白名单过滤（kwargs 会直接拼进 SQL）；列缺失时剔除该键而不是抛错 ——
    否则迁移未跑时``trace_run_id`` 会连带把 status 更新一起拖掉。
    """
    fields = {k: v for k, v in kwargs.items() if k in _STRATEGY_RUN_UPDATABLE}
    if not fields:
        return
    with get_backtest_db() as conn:
        missing = _missing_columns(conn, 'strategy_run', fields)
        for key in missing:
            fields.pop(key, None)
        if not fields:
            return
        sets = ', '.join(f'{k} = ?' for k in fields)
        values = list(fields.values()) + [run_uuid]
        conn.execute(f'UPDATE strategy_run SET {sets} WHERE run_uuid = ?', values)


def list_strategy_runs(limit: int = 50) -> List[Dict[str, Any]]:
    """策略研究任务列表（含最优轮次的 OOS 摘要，策略库视图）"""
    with get_backtest_db() as conn:
        rows = conn.execute(f'''
            SELECT r.*, it.round AS best_round, it.hypothesis AS best_hypothesis,
                   it.oos_metrics AS best_oos_metrics, it.is_metrics AS best_is_metrics,
                   it.overfit_flag AS best_overfit_flag, it.strategy_json AS best_strategy_json
            FROM strategy_run r
            LEFT JOIN strategy_iter it ON it.id = r.best_iter_id
            ORDER BY r.created_at DESC LIMIT ?
        ''', (limit,)).fetchall()
        runs = [dict(r) for r in rows]
        for r in runs:
            for f in ('best_oos_metrics', 'best_is_metrics'):
                if r.get(f):
                    try:
                        r[f] = json.loads(r[f])
                    except (json.JSONDecodeError, TypeError):
                        pass
        return runs


def _intent_check_path(run_uuid: str) -> 'Path':
    from pathlib import Path
    from utils.app_paths import get_app_dir
    return Path(get_app_dir()) / 'data' / 'strategy_intent_checks' / (str(run_uuid) + '.json')


def _append_intent_check(run_uuid: str, round_no: int, payload: Dict[str, Any]) -> None:
    """把一轮的意图核对结果写进 sidecar（失败不影响主流程）。"""
    import json as _json
    try:
        path = _intent_check_path(run_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            data = _json.loads(path.read_text(encoding='utf-8') or '{}')
        data[str(round_no)] = payload
        path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        logger.debug('意图核对 sidecar 写入失败（忽略）', exc_info=True)


def load_intent_checks(run_uuid: str) -> Dict[str, Any]:
    """读回某 run 的意图核对结果（API / 卡片用）。"""
    import json as _json
    try:
        path = _intent_check_path(run_uuid)
        if path.exists():
            return _json.loads(path.read_text(encoding='utf-8') or '{}')
    except Exception:
        logger.debug('意图核对 sidecar 读取失败（忽略）', exc_info=True)
    return {}


def insert_strategy_iter(run_uuid: str, round_no: int, hypothesis: Optional[str],
                         strategy_json: Optional[str], strategy_hash: Optional[str],
                         strategy_diff: Optional[str], compile_ok: int,
                         compile_error: Optional[str],
                         is_metrics: Optional[Dict[str, Any]],
                         oos_metrics: Optional[Dict[str, Any]],
                         overfit_flag: int = 0, verdict: Optional[str] = None,
                         model: Optional[str] = None,
                         intent_check: Optional[Dict[str, Any]] = None) -> int:
    """插入一轮迭代记录，返回新记录 id。

    intent_check（意图保真核对）**不走表结构**：先落 data/strategy_intent_checks/<run_uuid>.json
    的 sidecar —— 加列要动 migrator（见 utils/db_schema/tables/backtest.py 的 migrate 机制），
    在没跑通迁移前先不碰 schema，避免真实库写崩。
    """
    """插入一轮迭代记录，返回新记录 id"""
    run = get_strategy_run(run_uuid)
    if run is None:
        raise ValueError(f'策略研究任务不存在: {run_uuid}')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_backtest_db() as conn:
        cur = conn.execute('''INSERT INTO strategy_iter
            (run_id, round, hypothesis, strategy_json, strategy_hash, strategy_diff,
             compile_ok, compile_error, is_metrics, oos_metrics, overfit_flag,
             verdict, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (run['id'], round_no, hypothesis, strategy_json, strategy_hash,
             strategy_diff, compile_ok, compile_error,
             json.dumps(is_metrics, ensure_ascii=False) if is_metrics is not None else None,
             json.dumps(oos_metrics, ensure_ascii=False) if oos_metrics is not None else None,
             overfit_flag, verdict, model, now))
        iid = cur.lastrowid
    if intent_check:
        _append_intent_check(run_uuid, round_no, intent_check)
    return iid


def update_strategy_iter(iter_id: int, **kwargs) -> None:
    """更新迭代轮次字段（verdict 等）"""
    if not kwargs:
        return
    sets = ', '.join(f'{k} = ?' for k in kwargs)
    values = list(kwargs.values()) + [iter_id]
    with get_backtest_db() as conn:
        conn.execute(f'UPDATE strategy_iter SET {sets} WHERE id = ?', values)


def get_strategy_iters(run_uuid: str) -> List[Dict[str, Any]]:
    """按 run_uuid 获取全部轮次（round 升序），JSON 字段已反序列化"""
    with get_backtest_db() as conn:
        rows = conn.execute(f'''
            SELECT {_ITER_COLS} FROM strategy_iter
            WHERE run_id = (SELECT id FROM strategy_run WHERE run_uuid = ?)
            ORDER BY round, id
        ''', (run_uuid,)).fetchall()
    iters = [dict(r) for r in rows]
    # 意图核对结果存在 sidecar（data/strategy_intent_checks/<run_uuid>.json），不进表结构。
    # **必须在这里并回去**：面板/卡片读 /iters 时若拿不到，「本轮偏离用户原话」的徽章
    # 永远不显示，闸门就等于只写不读（2026-10-18 实测确认这个洞）。
    checks = load_intent_checks(run_uuid) or {}
    for it in iters:
        for f in ('is_metrics', 'oos_metrics'):
            if it.get(f):
                try:
                    it[f] = json.loads(it[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        ic = checks.get(str(it.get('round')))
        if ic:
            it['intent_check'] = ic
    return iters


def get_strategy_iter_by_hash(strategy_hash: str) -> Optional[Dict[str, Any]]:
    """按策略指纹取最近一轮迭代记录（策略研发中途的策略只存在这里，注册表重启即空）"""
    h = str(strategy_hash or '').strip()
    if not h:
        return None
    with get_backtest_db() as conn:
        sql = (f'SELECT {_ITER_COLS} FROM strategy_iter WHERE strategy_hash = ?'
               ' ORDER BY id DESC LIMIT 1')
        row = conn.execute(sql, (h,)).fetchone()
        if not row:
            return None
        it = dict(row)
        for f in ('is_metrics', 'oos_metrics'):
            if it.get(f):
                try:
                    it[f] = json.loads(it[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        return it


def get_strategy_iter_by_id(iter_id: int) -> Optional[Dict[str, Any]]:
    """按 id 获取单轮记录（adopt 时取最优策略 JSON 用）"""
    with get_backtest_db() as conn:
        row = conn.execute(f'SELECT {_ITER_COLS} FROM strategy_iter WHERE id = ?',
                           (iter_id,)).fetchone()
        if not row:
            return None
        it = dict(row)
        for f in ('is_metrics', 'oos_metrics'):
            if it.get(f):
                try:
                    it[f] = json.loads(it[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        return it


def get_strategy_iters_by_hash(strategy_hash: str) -> List[Dict[str, Any]]:
    """按策略 hash 反查研发迭代史（策略详情页「一个策略的一生」用）。

    与 get_strategy_iters(run_uuid) 的区别：那个按 run_uuid 找，这个跨 run 按 hash 找——
    策略被采纳进 strategies 表后只剩 strategy_hash（设计文档 §8），这是唯一反查路径。
    """
    if not strategy_hash:
        return []
    with get_backtest_db() as conn:
        rows = conn.execute(
            f'SELECT {_ITER_COLS} FROM strategy_iter WHERE strategy_hash = ? ORDER BY id',
            (strategy_hash,)).fetchall()
    iters = [dict(r) for r in rows]
    for it in iters:
        for f in ('is_metrics', 'oos_metrics'):
            if it.get(f):
                try:
                    it[f] = json.loads(it[f])
                except (json.JSONDecodeError, TypeError):
                    pass
    return iters


# ============================================================
# strategy_session CRUD（对话式策略研发，Plan 2 任务 2）
# ============================================================

# 白名单：只允许更新这些列，避免把任意 kwargs 拼进 SQL
_SESSION_UPDATABLE = {
    'status', 'idea', 'universe', 'period_start', 'period_end',
    'entry_text', 'exit_text', 'entry_timing', 'exit_timing', 'slots_json', 'plan_json',
    'opened_by', 'intent_confidence', 'intent_signals', 'run_uuid', 'turn_count',
}


def _slot_value(slots: Dict[str, Any], key: str, default: Any = '') -> Any:
    """取槽位值，兼容两种形状：

    - 扁平：{'universe': '600519'}（strategy_intent._extract_slots 产出）
    - 嵌套：{'universe': {'value': '600519', 'source': 'user'}}（strategy_gate._resolve_slots 产出）
    """
    v = (slots or {}).get(key)
    if isinstance(v, dict):
        v = v.get('value')
    if v is None or v == '':
        return default
    return v


def _slot_text(slots: Dict[str, Any], key: str, default: str = '') -> str:
    """槽位取字符串；列表/字典等非标量退回默认值"""
    v = _slot_value(slots, key, default)
    return default if isinstance(v, (list, tuple, dict)) else str(v)


def _session_period(slots: Dict[str, Any]) -> tuple:
    """会话起止日期：优先扁平 period_start/period_end，其次嵌套 period=[start, end]"""
    start = _slot_value(slots, 'period_start')
    end = _slot_value(slots, 'period_end')
    if not start or not end:
        period = _slot_value(slots, 'period')
        if isinstance(period, (list, tuple)) and len(period) >= 2:
            start, end = period[0], period[1]
    return str(start or ''), str(end or '')


def create_strategy_session(session_uuid: str, dialog_uuid: str, src_text: str,
                            idea: str = '', opened_by: str = 'rule',
                            intent_confidence: float = 0.0,
                            intent_signals: Optional[Dict[str, Any]] = None,
                            slots: Optional[Dict[str, Any]] = None,
                            status: str = 'pending_confirm') -> Dict[str, Any]:
    """创建策略会话（默认停在 pending_confirm：没确认前绝不起回测）"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    slots = slots or {}
    period_start, period_end = _session_period(slots)
    with get_backtest_db() as conn:
        # exit_timing 同backtest_runs：迁移未应用时降级为不写
        skip = _missing_columns(conn, 'strategy_session', ('exit_timing',))
        if skip:
            conn.execute('''INSERT INTO strategy_session
                (session_uuid, dialog_uuid, status, src_text, idea, universe, period_start, period_end,
                 entry_text, exit_text, entry_timing, slots_json, opened_by,
                 intent_confidence, intent_signals, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session_uuid, dialog_uuid, status, src_text[:500], idea[:500],
                 _slot_text(slots, 'universe'), period_start, period_end,
                 _slot_text(slots, 'entry'), _slot_text(slots, 'exit'),
                 _slot_text(slots, 'entry_timing', 'next_open'),
                 json.dumps(slots, ensure_ascii=False), opened_by, float(intent_confidence),
                 json.dumps(intent_signals or {}, ensure_ascii=False), now, now))
        else:
            conn.execute('''INSERT INTO strategy_session
            (session_uuid, dialog_uuid, status, src_text, idea, universe, period_start, period_end,
             entry_text, exit_text, entry_timing, exit_timing, slots_json, opened_by,
             intent_confidence, intent_signals, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session_uuid, dialog_uuid, status, src_text[:500], idea[:500],
                 _slot_text(slots, 'universe'), period_start, period_end,
                 _slot_text(slots, 'entry'), _slot_text(slots, 'exit'),
                 _slot_text(slots, 'entry_timing', 'next_open'),
                 _slot_text(slots, 'exit_timing', ''),
                 json.dumps(slots, ensure_ascii=False), opened_by, float(intent_confidence),
                 json.dumps(intent_signals or {}, ensure_ascii=False), now, now))
    # 注意：降级分支不能提前 return —— 那样会跳过上下文管理器的提交。
    return get_strategy_session(session_uuid)


def get_strategy_session(session_uuid: str) -> Optional[Dict[str, Any]]:
    """按 session_uuid 获取会话"""
    with get_backtest_db() as conn:
        row = conn.execute('SELECT * FROM strategy_session WHERE session_uuid = ?',
                           (session_uuid,)).fetchone()
        return dict(row) if row else None


def get_open_strategy_session(dialog_uuid: str) -> Optional[Dict[str, Any]]:
    """取该对话当前活着的会话（pending_confirm / active），无则 None。

    这是闸门的第一句查询——有会话就续轮，不再做意图判定。
    """
    with get_backtest_db() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_session WHERE dialog_uuid = ? "
            "AND status IN ('pending_confirm', 'active') "
            "ORDER BY updated_at DESC LIMIT 1", (dialog_uuid,)).fetchone()
        return dict(row) if row else None


def get_strategy_session_by_run(run_uuid: str) -> Optional[Dict[str, Any]]:
    """按 run_uuid 反查会话（含已归档）。

    用途：结果卡需要会话的 ``entry_timing``（成交口径）。前端「跑完自动升级」
    时只拿得到 run_uuid，不指望它知道当前对话；而 run 表本身不存口径。
    会话已归档（用户说了「结束会话」）时 open 版本查不到，这张表仍然能查到。
    """
    if not run_uuid:
        return None
    with get_backtest_db() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_session WHERE run_uuid = ? "
            "ORDER BY updated_at DESC LIMIT 1", (run_uuid,)).fetchone()
        return dict(row) if row else None


def update_strategy_session(session_uuid: str, **kwargs) -> None:
    """更新会话字段（白名单校验，避免把任意 kwargs 拼进 SQL）"""
    fields = {k: v for k, v in kwargs.items() if k in _SESSION_UPDATABLE}
    if not fields:
        return
    with get_backtest_db() as conn:
        # 迁移未应用时剔除不存在的列，避免整个 UPDATE 失败（连带丢 status 等关键字段）
        missing = _missing_columns(conn, 'strategy_session', fields)
        for key in missing:
            fields.pop(key, None)
        if not fields:
            return
        sets = ', '.join(k + ' = ?' for k in fields)
        values = list(fields.values()) + [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session_uuid]
        conn.execute('UPDATE strategy_session SET ' + sets + ', updated_at = ? WHERE session_uuid = ?', values)


def archive_strategy_session(session_uuid: str) -> None:
    """归档会话（status=archived）"""
    update_strategy_session(session_uuid, status='archived')


def get_latest_research_backtest(ref_id: str) -> Optional[Dict[str, Any]]:
    """取某次策略研发（strategy_run.run_uuid）最近一次回测的 run_id 与体检结论。

    研发链路与 backtest_runs 之间只有 origin='research' + ref_id=run_uuid 这条线索（Plan 1），
    故按它反查最近一次回测，再取体检结论（health / total_return）。
    """
    if not ref_id:
        return None
    with get_backtest_db() as conn:
        row = conn.execute(
            "SELECT run_id FROM backtest_runs WHERE origin = 'research' AND ref_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(ref_id),),
        ).fetchone()
    if not row:
        return None
    run_id = row['run_id'] if hasattr(row, 'keys') else row[0]
    result = get_backtest_result(run_id) or {}
    return {
        'run_id': run_id,
        'health': str(result.get('health') or ''),
        'health_detail': str(result.get('health_detail') or ''),
        'total_return': result.get('total_return'),
    }
