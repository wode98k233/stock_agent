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
import logging
import re
import sqlite3
import threading
import time

import pandas as pd
from config import Config
from utils.cache.db_utils import make_db_context

logger = logging.getLogger(__name__)

get_market_data_db = make_db_context(lambda: Config.get_market_data_db_path())


def init_market_data_tables():
    """初始化 market_data.db 表结构（DDL 统一来源 utils/db_schema）"""
    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.registry import ALL_DBS
    with get_market_data_db() as conn:
        ensure_schema(conn, ALL_DBS["market_data"].tables)


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
# 读取侧尾部补齐（缓存落后于最近已完成交易日时增量拉取）
# ============================================================

_TAIL_REFRESH_COOLDOWN_S = 600

# 冷启动（本地零行）时全量拉取的窗口（自然日）。**补尾不负责冷启动**
# （见 refresh_stale_tail 注释），此常量供采集侧 / 后台保活作业复用。
TAIL_COLD_START_DAYS = 250
_tail_refresh_lock = threading.Lock()
_tail_refresh_last: dict[str, float] = {}


def _tail_refresh_allowed(key: str, cooldown_s: int) -> bool:
    """尝试级冷却判定：允许则记录当前时间；冷却内返回 False。

    失败也占坑（调用方先记录再触发拉取），避免热路径反复打数据源。
    """
    now = time.time()
    with _tail_refresh_lock:
        if now - _tail_refresh_last.get(key, 0.0) < cooldown_s:
            return False
        _tail_refresh_last[key] = now
        return True


def refresh_stale_tail(code: str, *, market: str = "cn",
                       cooldown_s: int = _TAIL_REFRESH_COOLDOWN_S,
                       latest: Optional[str] = None, fetch=None) -> bool:
    """缓存尾部落后于最近已完成交易日时，拉取缺口并回写；冷却内/失败/异常 → False（静默）。

    读取侧统一补尾入口：K 线图路由、策略扫描（scan/scan_many）共用同一份
    新鲜度判定与节流，INSERT OR REPLACE 幂等覆盖，失败继续用本地旧缓存。

    Args:
        code: 标的代码（个股/指数/板块，随 fetch 而定）
        market: 交易日历市场，默认 cn
        cooldown_s: 同 code 最小重试间隔（秒）；尝试级，失败也记时间
        latest: 本地最新日期 'YYYY-MM-DD'；缺省查 stock_daily
        fetch: 注入的拉取函数（缺省 fetch_and_store_daily），签名 fetch(code, days=gap)

    Returns:
        True 表示本次确实拉取并写库（不保证一定补齐到最新，源可能仍滞后）。
    """
    try:
        from utils.trading_calendar import get_effective_trading_date

        if latest is None:
            latest = get_latest_trade_date(code)
        if not latest:
            # 本地零行 = **冷启动**，不归补尾管：本函数在 K 线图读取路径上，
            # 冷启动要全量拉数百天（实测单只可达 30s），塞进读路径会拖垮打开图表。
            # 冷启动由采集侧负责（`DataCollector._tail_fill` 的冷启动分支 /
            # 后台「K 线保活」作业），见 server/scan_jobs.kline_keepalive。
            return False
        latest = str(latest)
        effective = get_effective_trading_date(market).isoformat()
        if latest >= effective:
            return False   # 已是最新（ISO 字符串可直接比较）

        fetch_fn = fetch or fetch_and_store_daily
        key = f"{getattr(fetch_fn, '__name__', 'fetch')}:{code}"
        if not _tail_refresh_allowed(key, cooldown_s):
            return False

        # 缺口自然日 + 余量，最少 40 天（数据源按区间返回，40 天足够覆盖小缺口
        # 且满足 _ensure_enough_history_rows 的行数校验）
        gap_days = max(40, (datetime.now() - datetime.strptime(latest, "%Y-%m-%d")).days + 15)
        stored = fetch_fn(code, days=gap_days)
        logger.info("K线尾部补齐 %s: 本地最新 %s，目标 %s，拉取 %s 天入库 %s 条",
                    code, latest, effective, gap_days, stored)
        return True
    except Exception as e:
        logger.debug("K线尾部补齐跳过（继续用本地缓存） %s: %s", code, e)
        return False


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
        # ⚠️ 能力级重排必须显式施加：`DataSourceManager.order_sources` 只在 `@retry()`
        # 装饰器路径里被调用（tools/fetcher/base.py），而本函数是**手写源循环**、
        # 绕过了装饰器 —— 不补这一行就会按基础 priority 取源，让 `mx_data`(110) 打头。
        # 它对 hist 是表格查询、只返回快照（实测 000063 仅 1 行），必然失败再回退，
        # 单只白耗 ~30s。2026-09-30 实测：加上后单只 33s → 见下方实测。
        candidates = DataSourceManager.order_sources(candidates, "hist")

    errors = []
    for target in candidates:
        try:
            raw = target.get_stock_hist(code, period="daily", start=start, end=end)
            if raw is None or raw.empty:
                raise ValueError("返回空数据")
            df = stock_data._normalize_history_df(raw, code).tail(days)
            # ⚠️ 行数校验必须**放在循环内**：部分源（如 mx_data 表格查询）会返回
            # 「非空的快照」——对 000063 实测只给 1 行。若只在循环外校验（原实现），
            # 这种源会以非空身份被 return、短路整条优先级链，随后在循环外抛
            # 「K 线过少」且**不再回退**，导致排在其后的 sina/akshare 等可用源永远轮不到。
            # 2026-09-30 实测：67/74 只自选持仓标的因此补尾失败、K 线停在数周前。
            _ensure_enough_history_rows(df, days, code, target.name)
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


def get_index_daily_count(code: str) -> int:
    """获取指数日线数据条数（与 get_stock_daily_count 对称）"""
    sql = "SELECT COUNT(*) as cnt FROM index_daily WHERE code = ?"
    with get_market_data_db() as conn:
        row = conn.execute(sql, (code,)).fetchone()
        return row['cnt'] if row else 0


def get_index_name(code: str) -> Optional[str]:
    """从 index_info 取指数名称（查不到返回 None）。

    与 get_stock_info 不同：这里只需要名称，不做整行查询，供标的类型判定
    的高频调用使用（一次 SELECT name）。
    """
    try:
        with get_market_data_db() as conn:
            row = conn.execute("SELECT name FROM index_info WHERE code = ?", (code,)).fetchone()
            return row['name'] if row else None
    except Exception:
        return None


def is_index_code(code: str) -> bool:
    """裸码是否存在于指数表（index_info）"""
    return bool(code) and get_index_name(str(code)) is not None


def resolve_symbol_market(code: str, name_hint: str = "") -> str:
    """判定裸码应从哪张表取数，返回 'INDEX'（index_daily）或 'STOCK'（stock_daily）。

    背景：`000001` 既是上证指数（index_daily）又是平安银行（stock_daily），
    同码不同标的在两表各存一份；不显式判定就会静默取到同码个股
    （价格、名称全错且不报错）。实测 index_info 与 stock_info 的交集**只有 000001**。

    规则（按可信度递减）：
      1. name_hint 与 index_info 的名称一致 → INDEX（名称来自数据源表头，最可信；
         如 chart fragment 的 stock_name="上证指数"）
      2. 只在 index_info 有 → INDEX（399001 深证成指 / 000300 沪深300 等无同码个股）
      3. 只在 stock_info 有 → STOCK
      4. 两边都有且 name_hint 未命中指数名（实测仅 000001）→ STOCK（保守，保持既有
         行为）。市场类报告由调用方按场景显式传 market='INDEX' 覆盖。
    """
    code = str(code or "").strip()
    if not code:
        return "STOCK"
    idx_name = get_index_name(code)
    if not idx_name:
        return "STOCK"
    hint = str(name_hint or "").strip()
    if hint and hint == str(idx_name).strip():
        return "INDEX"
    try:
        with get_market_data_db() as conn:
            row = conn.execute("SELECT 1 FROM stock_info WHERE code = ?", (code,)).fetchone()
    except Exception:
        return "INDEX"
    return "STOCK" if row else "INDEX"


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


def _fetch_hk_index_daily(ak, symbol: str):
    """港股指数日线：**新浪优先，东财兜底**。

    为什么不是东财优先（原实现）：
    1. `ak.stock_hk_index_daily_em` 每次调用都要先分页拉 `stock_hk_index_spot_em`
       建代码字典（东财 15.push2 接口），该调用在受限网络下极易被掐断
       （2026-09-18 实测 ProxyError RemoteDisconnected），且每次调用都重复建字典。
    2. 东财该接口只返回 date/open/high/low/latest 五列，**没有成交量/成交额**；
       新浪返回 7 列（含 volume/amount）。
    3. 恒生科技相关研究（74 个月实证）的取数口径就是新浪财经，优先新浪可保证
       台账读数与研究结论同源可比。

    返回 DataFrame（列名保持原始，由调用方统一 col_map 规范化）。
    """
    errors = []
    for label, fn in (("新浪", lambda: ak.stock_hk_index_daily_sina(symbol=symbol)),
                      ("东财", lambda: ak.stock_hk_index_daily_em(symbol=symbol))):
        try:
            df = fn()
        except Exception as e:  # 单源失败不致命，换下一个源
            errors.append(f"{label}: {type(e).__name__}")
            continue
        if df is not None and not df.empty:
            return df
        errors.append(f"{label}: 空结果")
    raise RuntimeError(f"港股指数 {symbol} 所有数据源均失败（{'；'.join(errors)}）")


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
    from tools.fetcher.akshare_ds import AkshareDataSource, _is_csindex_code
    from tools.fetcher import ak_index_hist_csindex
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

    # 代码类型判定（提到跳过分支之前，供 source 标记复用）：
    # 中证指数（H + 纯数字，如 H30269 红利低波）必须优先判定——原先
    # `startswith('H')` 的粗判会把它当港股指数送去 stock_hk_index_daily_em，
    # 必然空表。港股指数是 H + 字母（HSTECH / HSI）。
    is_csindex = _is_csindex_code(code)
    is_hk = (not is_csindex) and code.startswith(('H', 'hk'))
    src = 'csindex' if is_csindex else 'akshare'

    if existing_count >= days and existing_latest >= today:
        if name:
            upsert_index_info([{'code': code, 'name': name}], source=src)
        return 0

    # 转换代码格式
    if is_csindex:
        symbol = code.strip().upper()
    elif is_hk:
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
            if is_csindex:
                # 中证指数走公司自有接口；无「条数」参数，按天数换算起止日
                from datetime import timedelta as _td
                start = (_date.today() - _td(int(days * 1.5))).strftime('%Y%m%d')
                df = ak_index_hist_csindex(symbol, start, '')
            elif is_hk:
                df = _fetch_hk_index_daily(ak, symbol)
            else:
                df = ak.stock_zh_index_daily_em(symbol=symbol)

            if df is None or df.empty:
                return 0

            # 规范化列名（中证指数出口已统一为 report_date，一并映射）
            # `latest` 是港股指数接口的收盘价列名（akshare stock_hk_index_daily_em 返回
            # date/open/high/low/latest 五列，没有 close、也没有量额）——漏这条映射会让
            # 每一条港股指数日线都静默落库成 close=NULL（行数正常、无报错，只是算不出任何
            # 收益率/均线）。2026-09-18 实测 HSTECH/HSI 各 300 行全为 NULL 才发现。
            col_map = {
                '日期': 'trade_date', 'date': 'trade_date', 'report_date': 'trade_date',
                '开盘': 'open', 'open': 'open',
                '最高': 'high', 'high': 'high',
                '最低': 'low', 'low': 'low',
                '收盘': 'close', 'close': 'close', 'latest': 'close',
                '成交量': 'volume', 'volume': 'volume',
                '成交额': 'amount', '成交金额': 'amount', 'amount': 'amount',
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
                    upsert_index_info([{'code': code, 'name': name}], source=src)
                return 0

            # 计算涨跌幅（源已提供时保留源值：tail 截断后重算会让首行变 NaN）
            if 'close' in df.columns:
                if 'pct_change' not in df.columns:
                    df['pct_change'] = df['close'].pct_change() * 100
                if 'change_amount' not in df.columns:
                    df['change_amount'] = df['close'].diff()

            records = df.to_dict('records')
            for r in records:
                r['code'] = code

            count = upsert_index_daily(records, source=src)

            # 写入 index_info
            if name:
                upsert_index_info([{'code': code, 'name': name}], source=src)

            return count

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 3  # 3s, 6s, 9s
                _time.sleep(wait)

    raise RuntimeError(f'指数 {code} 采集失败（重试{max_retries}次）: {last_error}')


# ============================================================
# 板块K线（P6 第二步：行业基准覆盖管道）
# ============================================================

_BOARD_CODE_RE = re.compile(r'^board_(industry|concept)_(.+)$')


def fetch_and_store_board_kline(board_code: str, days: int = 500,
                                max_retries: int = 3) -> int:
    """从数据源获取板块K线并存储（增量 + 重试）。

    板块 K 线存 stock_daily（code = 'board_industry_半导体'，沿用
    backtest/data_collector.py::_collect_board_kline 的既有约定），
    行业基准映射（signal_eval/benchmarks.py）按该 code 消费。

    days 为行数语义（与 fetch_and_store_index_daily 一致）：
    首次采集回溯 days*1.5 自然日（≈ days 交易日）；已有数据只补缺口
    （起点前移 7 天重叠，保证首行 pct_change 有基准）。
    """
    import time as _time
    from datetime import timedelta

    m = _BOARD_CODE_RE.match((board_code or '').strip())
    if not m:
        raise ValueError(f'非法板块代码: {board_code!r}（期望 board_industry_半导体 格式）')
    btype, symbol = m.group(1), m.group(2)

    # 1. 现有数据检查
    existing_latest = None
    with get_market_data_db() as conn:
        row = conn.execute(
            'SELECT MAX(trade_date) as latest FROM stock_daily WHERE code = ?',
            (board_code,)
        ).fetchone()
        if row and row['latest']:
            existing_latest = str(row['latest'])
        # 板块名 → BK 代码（hist 接口直接认 BK，跳过其内部名称反查请求）
        try:
            bk_row = conn.execute(
                'SELECT board_code FROM stock_board WHERE board_name = ? AND board_type = ?',
                (symbol, btype)).fetchone()
            if bk_row and bk_row['board_code']:
                symbol = bk_row['board_code']
        except sqlite3.Error:
            pass  # 库里无 stock_board 表（测试库）→ 用板块名请求

    from datetime import date as _date
    today = _date.today()
    if existing_latest and existing_latest >= today.strftime('%Y-%m-%d'):
        return 0

    if existing_latest:
        start = (datetime.strptime(existing_latest, '%Y-%m-%d') - timedelta(days=7)).strftime('%Y%m%d')
    else:
        start = (today - timedelta(days=int(days * 1.5))).strftime('%Y%m%d')
    end = today.strftime('%Y%m%d')

    # 2. 带重试取数（复用 AkshareDataSource 通道：rate limit + em patch）
    from tools.fetcher.akshare_ds import AkshareDataSource
    AkshareDataSource._ensure_patch()
    last_error = None
    for attempt in range(max_retries):
        try:
            df = AkshareDataSource.get_board_industry_hist(
                symbol=symbol, period='日k', start=start, end=end)
            if df is None or df.empty:
                return 0

            # 列归一化（东财板块 hist：日期/开盘/收盘/最高/最低/成交量/成交额/换手率/振幅）
            col_map = {
                '日期': 'trade_date', 'date': 'trade_date',
                '开盘': 'open', 'open': 'open',
                '最高': 'high', 'high': 'high',
                '最低': 'low', 'low': 'low',
                '收盘': 'close', 'close': 'close',
                '成交量': 'volume', 'volume': 'volume',
                '成交额': 'amount', 'amount': 'amount',
                '换手率': 'turnover_rate', 'turnover_rate': 'turnover_rate',
                '振幅': 'amplitude', 'amplitude': 'amplitude',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            if 'trade_date' not in df.columns or 'close' not in df.columns:
                return 0

            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df = df.sort_values('trade_date')

            # 涨跌幅在全量序列上先算（重叠数据做基准），再截尾、再做增量过滤
            if 'close' in df.columns:
                df['pct_change'] = df['close'].pct_change() * 100
                df['change_amount'] = df['close'].diff()
            df = df.tail(days)
            if existing_latest:
                df = df[df['trade_date'] > existing_latest]
            if df.empty:
                return 0

            records = df.to_dict('records')
            for r in records:
                r['code'] = board_code
            return upsert_stock_daily(records, source='akshare')

        except RuntimeError as e:
            # 数据源明确"无数据"（板块新建/无行情）→ 不重试
            if '未获取到' in str(e):
                return 0
            last_error = e
            if attempt < max_retries - 1:
                _time.sleep((attempt + 1) * 3)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                _time.sleep((attempt + 1) * 3)

    raise RuntimeError(f'板块 {board_code} K线采集失败（重试{max_retries}次）: {last_error}')
