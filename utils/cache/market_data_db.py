"""market_data.db 连接管理和表初始化

职责：
- stock_info: 股票基本信息
- stock_board: 板块维表
- stock_board_member: 股票-板块关系表
- stock_daily: 个股日线
- stock_adjust_factor: 复权因子
"""
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import pandas as pd
from config import Config
from utils.cache.db_utils import make_db_context

get_market_data_db = make_db_context(lambda: Config.get_market_data_db_path())


def init_market_data_tables():
    """初始化 market_data.db 表结构"""
    with get_market_data_db() as conn:
        c = conn.cursor()

        # 股票基本信息
        c.execute('''CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            exchange TEXT,
            list_date TEXT,
            delist_date TEXT,
            stock_type TEXT,
            status TEXT,
            is_st INTEGER DEFAULT 0,
            total_share REAL,
            float_share REAL,
            source TEXT,
            updated_at TEXT,
            raw_json TEXT
        )''')

        # 板块维表
        c.execute('''CREATE TABLE IF NOT EXISTS stock_board (
            board_id TEXT PRIMARY KEY,
            board_code TEXT,
            board_name TEXT NOT NULL,
            board_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            parent_board_id TEXT,
            is_active INTEGER DEFAULT 1,
            updated_at TEXT,
            raw_json TEXT,
            UNIQUE(provider, board_type, board_code)
        )''')

        # 股票-板块关系表
        c.execute('''CREATE TABLE IF NOT EXISTS stock_board_member (
            board_id TEXT NOT NULL,
            code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            is_primary INTEGER DEFAULT 0,
            weight REAL,
            rank INTEGER,
            reason TEXT,
            source TEXT,
            updated_at TEXT,
            raw_json TEXT,
            PRIMARY KEY (board_id, code, as_of_date)
        )''')

        # 个股日线
        c.execute('''CREATE TABLE IF NOT EXISTS stock_daily (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            pre_close REAL,
            pct_change REAL,
            change_amount REAL,
            volume REAL,
            amount REAL,
            turnover_rate REAL,
            amplitude REAL,
            trade_status TEXT,
            is_st INTEGER,
            source TEXT,
            updated_at TEXT,
            raw_json TEXT,
            PRIMARY KEY (code, trade_date)
        )''')

        # 复权因子
        c.execute('''CREATE TABLE IF NOT EXISTS stock_adjust_factor (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            adjust_factor REAL,
            fore_adjust_factor REAL,
            back_adjust_factor REAL,
            source TEXT,
            updated_at TEXT,
            PRIMARY KEY (code, trade_date)
        )''')

        # 创建索引
        c.execute('CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(trade_date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_daily_code_date_desc ON stock_daily(code, trade_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_board_member_code_date ON stock_board_member(code, as_of_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_board_member_board_date ON stock_board_member(board_id, as_of_date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_board_type_name ON stock_board(board_type, board_name)')

        # ── 指数表（独立于个股） ──

        c.execute('''CREATE TABLE IF NOT EXISTS index_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT,
            updated_at TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS index_daily (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            pct_change REAL,
            change_amount REAL,
            source TEXT,
            updated_at TEXT,
            PRIMARY KEY (code, trade_date)
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(trade_date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_index_daily_code_date ON index_daily(code, trade_date DESC)')


# ============================================================
# stock_daily 数据操作
# ============================================================

def upsert_stock_daily(records: List[Dict[str, Any]], source: str = "akshare") -> int:
    """批量插入/更新日线数据

    Args:
        records: 记录列表，每条包含 code, trade_date, open, high, low, close, volume, amount 等
        source: 数据来源标识

    Returns:
        插入/更新的记录数
    """
    if not records:
        return 0

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sql = '''INSERT OR REPLACE INTO stock_daily
             (code, trade_date, open, high, low, close, pre_close, pct_change,
              change_amount, volume, amount, turnover_rate, amplitude,
              trade_status, is_st, source, updated_at, raw_json)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''

    with get_market_data_db() as conn:
        c = conn.cursor()
        for r in records:
            # 规范化日期格式，确保主键一致
            trade_date = _normalize_trade_date(r.get('trade_date'))
            c.execute(sql, (
                r.get('code'),
                trade_date,
                r.get('open'),
                r.get('high'),
                r.get('low'),
                r.get('close'),
                r.get('pre_close'),
                r.get('pct_change'),
                r.get('change_amount'),
                r.get('volume'),
                r.get('amount'),
                r.get('turnover_rate'),
                r.get('amplitude'),
                r.get('trade_status', 'active'),
                r.get('is_st', 0),
                source,
                now,
                r.get('raw_json')
            ))
        return len(records)


def get_stock_daily(code: str, start_date: str = None, end_date: str = None,
                    limit: int = None) -> pd.DataFrame:
    """查询股票日线数据

    Args:
        code: 股票代码
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
        limit: 限制条数（从最新开始倒数）

    Returns:
        DataFrame，按日期升序排列
    """
    conditions = ["code = ?"]
    params = [code]

    if start_date:
        conditions.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("trade_date <= ?")
        params.append(end_date)

    where_clause = " AND ".join(conditions)
    order_limit = "ORDER BY trade_date ASC"
    if limit and not start_date and not end_date:
        # 如果没有日期限制但有条数限制，取最近N条
        order_limit = f"ORDER BY trade_date DESC LIMIT {limit}"

    sql = f"""SELECT code, trade_date, open, high, low, close, pre_close,
                     pct_change, change_amount, volume, amount, turnover_rate,
                     amplitude, trade_status
              FROM stock_daily
              WHERE {where_clause}
              {order_limit}"""

    with get_market_data_db() as conn:
        df = pd.read_sql_query(sql, conn, params=params)

    # 如果是倒序查询，需要翻转回来
    if limit and not start_date and not end_date:
        df = df.iloc[::-1].reset_index(drop=True)

    return df


def get_latest_trade_date(code: str) -> Optional[str]:
    """获取股票最新交易日期"""
    sql = "SELECT MAX(trade_date) as max_date FROM stock_daily WHERE code = ?"
    with get_market_data_db() as conn:
        c = conn.cursor()
        c.execute(sql, (code,))
        row = c.fetchone()
        return row['max_date'] if row else None


def get_stock_daily_count(code: str) -> int:
    """获取股票日线数据条数"""
    sql = "SELECT COUNT(*) as cnt FROM stock_daily WHERE code = ?"
    with get_market_data_db() as conn:
        c = conn.cursor()
        c.execute(sql, (code,))
        row = c.fetchone()
        return row['cnt'] if row else 0


# ============================================================
# stock_info 数据操作
# ============================================================

def upsert_stock_info(info: Dict[str, Any], source: str = "akshare") -> None:
    """插入/更新股票基本信息"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sql = '''INSERT OR REPLACE INTO stock_info
             (code, name, market, exchange, list_date, delist_date, stock_type,
              status, is_st, total_share, float_share, source, updated_at, raw_json)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''

    with get_market_data_db() as conn:
        conn.execute(sql, (
            info.get('code'),
            info.get('name'),
            info.get('market'),
            info.get('exchange'),
            info.get('list_date'),
            info.get('delist_date'),
            info.get('stock_type'),
            info.get('status', 'active'),
            info.get('is_st', 0),
            info.get('total_share'),
            info.get('float_share'),
            source,
            now,
            info.get('raw_json')
        ))


def get_stock_info(code: str) -> Optional[Dict[str, Any]]:
    """获取股票基本信息"""
    sql = "SELECT * FROM stock_info WHERE code = ?"
    with get_market_data_db() as conn:
        c = conn.cursor()
        c.execute(sql, (code,))
        row = c.fetchone()
        return dict(row) if row else None


# ============================================================
# 数据采集辅助
# ============================================================

def fetch_and_store_daily(code: str, days: int = 250, logger=None, source: str = "auto") -> int:
    """从数据源获取日线数据并存储到 market_data.db

    Args:
        code: 股票代码
        days: 获取天数
        logger: 日志器

    Returns:
        存储的记录数

    Raises:
        ValueError: 当返回的数据条数过少时
    """
    if logger:
        logger.info(f"📥 获取 {code} 最近 {days} 天日线数据...")

    # 获取数据（已经有缓存机制）
    df, used_source = _fetch_history_for_market_data(code, days, source=source, logger=logger)
    if df.empty:
        if logger:
            logger.warning(f"⚠️ {code} 无数据")
        return 0

    # 校验数据条数是否足够（防止将单条行情写成历史日线）
    _ensure_enough_history_rows(df, days, code, used_source or source)

    # 同时写入 stock_info（如果不存在）
    _ensure_stock_info(code, df, used_source or source or "auto")

    # 按日期排序，确保计算 pre_close 正确
    df = df.sort_index()

    records = []
    prev_close = None
    for _, row in df.iterrows():
        trade_date = _normalize_trade_date(row.get('date', row.name))
        close = row.get('close')

        # 计算 pre_close、pct_change、change_amount（如果数据源未提供）
        pre_close = row.get('pre_close')
        if pre_close is None and prev_close is not None:
            pre_close = prev_close

        pct_change = row.get('pct_change', row.get('change_pct'))
        if pct_change is None and pre_close and pre_close > 0 and close:
            pct_change = ((close - pre_close) / pre_close) * 100

        change_amount = row.get('change_amount', row.get('change'))
        if change_amount is None and pre_close and close:
            change_amount = close - pre_close

        records.append({
            'code': code,
            'trade_date': trade_date,
            'open': row.get('open'),
            'high': row.get('high'),
            'low': row.get('low'),
            'close': close,
            'pre_close': pre_close,
            'pct_change': round(pct_change, 2) if pct_change is not None else None,
            'change_amount': round(change_amount, 2) if change_amount is not None else None,
            'volume': row.get('volume'),
            'amount': row.get('amount'),
            'turnover_rate': row.get('turnover_rate'),
            'amplitude': row.get('amplitude'),
        })

        # 更新前一日收盘价
        if close is not None:
            prev_close = close

    count = upsert_stock_daily(records, source=used_source or source or "auto")
    if logger:
        logger.info(f"✅ {code} 存储 {count} 条日线数据")

    return count


def _ensure_stock_info(code: str, df: pd.DataFrame, source: str) -> None:
    """确保 stock_info 表中有该股票的基本信息"""
    existing = get_stock_info(code)
    if existing:
        # 如果已存在但名称是代码，尝试更新名称
        if existing.get('name') == code:
            name = _fetch_stock_name(code)
            if name and name != code:
                upsert_stock_info({
                    'code': code,
                    'name': name,
                    'market': existing.get('market', _detect_market(code)),
                    'status': existing.get('status', 'active'),
                }, source=source)
        return

    # 根据代码推断市场
    market = _detect_market(code)

    # 尝试从数据中获取名称（某些数据源可能包含）
    name = None
    if 'name' in df.columns:
        name = df['name'].iloc[0] if not df['name'].empty else None

    # 如果没有名称，尝试从数据源获取
    if not name or name == code:
        name = _fetch_stock_name(code)

    upsert_stock_info({
        'code': code,
        'name': name or code,  # 如果没有名称，使用代码
        'market': market,
        'status': 'active',
    }, source=source)


def _fetch_stock_name(code: str) -> Optional[str]:
    """从数据源获取股票名称"""
    try:
        from tools.stock_data import get_stock_realtime
        return get_stock_realtime(code).get('name')
    except Exception:
        return None


def _detect_market(code: str) -> str:
    """根据代码推断市场类型

    支持：个股、ETF、板块K线
    """
    # 板块K线：board_industry_黄金、board_concept_人工智能
    if code.startswith('board_'):
        return 'board'
    # ETF：沪市 51/52/56/58，深市 15/16/18
    if code.startswith(('51', '52', '56', '58', '15', '16', '18')):
        return 'ETF'
    # 个股
    if code.startswith(('6', '9')):
        return 'SH'
    elif code.startswith(('0', '3')):
        return 'SZ'
    elif code.startswith(('4', '8')):
        return 'BJ'
    return 'UNKNOWN'


def _min_kline_rows(days: int) -> int:
    if days <= 5:
        return 1
    return min(days, min(20, max(5, days // 4)))


def _ensure_enough_history_rows(df: pd.DataFrame, days: int, code: str, source: str) -> None:
    min_rows = _min_kline_rows(days)
    if len(df) < min_rows:
        raise ValueError(f"{source or 'auto'} 返回 {code} K 线过少: {len(df)} < {min_rows}")


def _fetch_history_for_market_data(code: str, days: int, source: str = "auto", logger=None):
    """按数据源拉取历史 K 线，供 market_data.db 持久化使用。"""
    from tools import fetcher
    from tools.fetcher.base import DataSourceManager
    import tools.stock_data as stock_data

    ensure_initialized = getattr(fetcher, "_ensure_initialized", None)
    if ensure_initialized:
        ensure_initialized()

    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=days + 60)).strftime('%Y%m%d')
    source_name = (source or "auto").strip()
    market = DataSourceManager.detect_market(code)

    if source_name and source_name != "auto":
        candidates = [
            s for s in DataSourceManager.get_available_sources()
            if s.name == source_name
        ]
        if not candidates:
            raise ValueError(f"数据源不可用: {source_name}")
    else:
        candidates = DataSourceManager.get_sources_for_market(market)

    errors = []
    for target in candidates:
        try:
            raw = target.get_stock_hist(code, period="daily", start=start, end=end)
            if raw is None or raw.empty:
                raise ValueError("返回空数据")
            df = stock_data._normalize_history_df(raw, code).tail(days)
            if logger:
                logger.info(f"{code} K 线使用数据源 {target.name}，{len(df)} 条")
            return df, target.name
        except Exception as e:
            errors.append(f"{target.name}: {e}")
            if logger:
                logger.warning(f"{target.name} 获取 {code} K 线失败: {e}")

    raise ValueError(f"{code} K 线同步失败，可用数据源均不可用: {'; '.join(errors)}")


def _normalize_trade_date(value) -> str:
    """将数据源日期统一为 YYYY-MM-DD。"""
    if isinstance(value, pd.Timestamp):
        return value.strftime('%Y-%m-%d')
    parsed = pd.to_datetime(value, errors='coerce')
    if pd.notna(parsed):
        return parsed.strftime('%Y-%m-%d')
    return str(value)


# ============================================================
# index_daily / index_info 数据操作
# ============================================================

def upsert_index_daily(records: List[Dict[str, Any]], source: str = "akshare") -> int:
    """批量插入/更新指数日线数据"""
    if not records:
        return 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sql = '''INSERT OR REPLACE INTO index_daily
             (code, trade_date, open, high, low, close, volume, amount,
              pct_change, change_amount, source, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
    with get_market_data_db() as conn:
        for r in records:
            trade_date = _normalize_trade_date(r.get('trade_date'))
            conn.execute(sql, (
                r.get('code'), trade_date,
                r.get('open'), r.get('high'), r.get('low'), r.get('close'),
                r.get('volume'), r.get('amount'),
                r.get('pct_change'), r.get('change_amount'),
                source, now,
            ))
        conn.commit()
    return len(records)


def get_index_daily(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取指数日线数据"""
    with get_market_data_db() as conn:
        sql = 'SELECT * FROM index_daily WHERE code = ?'
        params = [code]
        if start_date:
            sql += ' AND trade_date >= ?'
            params.append(start_date)
        if end_date:
            sql += ' AND trade_date <= ?'
            params.append(end_date)
        sql += ' ORDER BY trade_date'
        return pd.read_sql_query(sql, conn, params=params)


def upsert_index_info(records: List[Dict[str, Any]], source: str = "akshare") -> int:
    """批量插入/更新指数基本信息"""
    if not records:
        return 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_market_data_db() as conn:
        for r in records:
            conn.execute('''INSERT OR REPLACE INTO index_info
                (code, name, source, updated_at) VALUES (?, ?, ?, ?)''',
                (r.get('code'), r.get('name'), source, now))
        conn.commit()
    return len(records)


def fetch_and_store_index_daily(code: str, name: str = '', days: int = 500,
                                max_retries: int = 3) -> int:
    """从数据源获取指数日线并存储（增量 + 重试）

    先查库里的最新日期，只补采缺失的部分。
    A股指数: ak.stock_zh_index_daily_em (sh000001 格式)
    港股指数: ak.stock_hk_index_daily_em (HSTECH 格式)
    """
    import time as _time
    from datetime import timedelta

    # 启用 eastmoney 补丁（fake_ua + cookie + 延迟）
    from tools.fetcher.akshare_ds import AkshareDataSource
    AkshareDataSource._ensure_patch()
    import akshare as ak

    # 1. 先查库里已有数据
    existing_latest = None
    existing_earliest = None
    existing_count = 0
    with get_market_data_db() as conn:
        row = conn.execute(
            'SELECT MAX(trade_date) as latest, MIN(trade_date) as earliest, COUNT(*) as cnt FROM index_daily WHERE code = ?',
            (code,)
        ).fetchone()
        if row and row['cnt'] > 0:
            existing_latest = row['latest']
            existing_earliest = row['earliest']
            existing_count = row['cnt']

    # 如果已有足够数据且最新日期是今天附近，跳过
    from datetime import date as _date
    today = _date.today().strftime('%Y-%m-%d')
    if existing_count >= days and existing_latest >= today:
        if name:
            upsert_index_info([{'code': code, 'name': name}], source='akshare')
        return 0

    # 2. 判断是港股还是A股
    is_hk = code.startswith(('H', 'hk'))

    # 转换代码格式
    if is_hk:
        symbol = code.lstrip('hk') if code.startswith('hk') else code
    else:
        symbol = code
        if not code.startswith(('sh', 'sz')):
            if code.startswith('0'):
                symbol = 'sh' + code
            elif code.startswith(('3', '9')):
                symbol = 'sz' + code

    # 3. 带重试的 API 调用
    last_error = None
    for attempt in range(max_retries):
        try:
            if is_hk:
                df = ak.stock_hk_index_daily_em(symbol=symbol)
            else:
                df = ak.stock_zh_index_daily_em(symbol=symbol)

            if df is None or df.empty:
                return 0

            # 规范化列名
            col_map = {
                '日期': 'trade_date', 'date': 'trade_date',
                '开盘': 'open', 'open': 'open',
                '最高': 'high', 'high': 'high',
                '最低': 'low', 'low': 'low',
                '收盘': 'close', 'close': 'close',
                '成交量': 'volume', 'volume': 'volume',
                '成交额': 'amount', 'amount': 'amount',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

            if 'trade_date' in df.columns:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.sort_values('trade_date')

            # 4. 只保留最近 N 天
            if 'trade_date' in df.columns:
                df = df.tail(days)

            # 5. 增量过滤：去掉库里已有的日期，只入库新数据
            if existing_latest and 'trade_date' in df.columns:
                df = df[df['trade_date'] > existing_latest]

            if df.empty:
                if name:
                    upsert_index_info([{'code': code, 'name': name}], source='akshare')
                return 0

            # 计算涨跌幅
            if 'close' in df.columns:
                df['pct_change'] = df['close'].pct_change() * 100
                df['change_amount'] = df['close'].diff()

            records = df.to_dict('records')
            for r in records:
                r['code'] = code

            count = upsert_index_daily(records, source='akshare')

            # 写入 index_info
            if name:
                upsert_index_info([{'code': code, 'name': name}], source='akshare')

            return count

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 3  # 3s, 6s, 9s
                _time.sleep(wait)

    raise RuntimeError(f'指数 {code} 采集失败（重试{max_retries}次）: {last_error}')
