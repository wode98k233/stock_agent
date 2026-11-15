#!/usr/bin/env python
"""新环境行情数据回填总入口。

目标：一条命令把「股票/ETF 清单 + 日线历史 + 指数 + 指数成分」补齐，并在
结束后跑一次覆盖审计。**默认不写业务表，只补 market_data.db 的行情/元数据。**

两条路径（自动选择）：
1. 有 ``HITHINK_FINANCE_API_KEY`` + ``pyarrow``：走同花顺官方全市场 Parquet
   ``--dump daily-k``（约 10 年、~945 万行，3 次请求），最快；默认只入库
   ``--since 2024-01-01`` 之后（下载仍是全量，接口没有日期区间参数）。
2. 没有 key：走逐只 ``fetch_and_store_daily``（akshare/hithink 自动切换），
   慢但不需要额外配置。

为什么需要它：2026-10-20 审计显示全库 5,504/5,590 只只有 1~30 根，其中
5,437 只恰好 20 根（2026-08-27~2026-09-30 的一次尾部导入）。策略池子研究
必须先补历史，否则 300 只池子里绝大多数会落进「无法评判」。

用法：
    # 先看计划（不联网、不写库）
    python scripts/ops/bootstrap_market_data.py --dry-run --limit 5

    # 批量回填（默认 since=2024-01-01，只入库 2024 年至今）
    python scripts/ops/bootstrap_market_data.py --mode dump --dump-kind daily-k --audit

    # 逐只回填（无 hithink key 时的默认路径）
    python scripts/ops/bootstrap_market_data.py --mode per-code --source akshare --since 2024-01-01

    # 只补指定标的
    python scripts/ops/bootstrap_market_data.py --codes 600519,300750,510300
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STATUS = ROOT / 'output' / 'backfill_status.json'
DEFAULT_MIN_BARS = 250
DEFAULT_YEARS = 2.0
DEFAULT_SLEEP = 0.2
DEFAULT_WORKERS = 1
DEFAULT_RETRIES = 2


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------
def lookback_days(years: float, min_bars: int = DEFAULT_MIN_BARS,
                  buffer_days: int = 30) -> int:
    """回填窗口天数：按年数换算，且至少覆盖 ``min_bars`` 个交易日。

    数据源 ``fetch_and_store_daily(days=N)`` 只支持「最近 N 天」，上限约 2000。
    """
    by_years = int(max(0.1, float(years or 0)) * 365) + int(buffer_days)
    by_bars = int(max(1, min_bars) * 7 / 5) + int(buffer_days)
    return max(60, min(2000, max(by_years, by_bars)))


def window_start(days: int, today: Optional[date] = None) -> str:
    ref = today or date.today()
    return (ref - timedelta(days=max(1, int(days)))).isoformat()


def plan_codes(codes: Iterable[str], bar_counts: Dict[str, int], *,
               min_bars: int, force: bool = False, limit: int = 0) -> Dict[str, List[str]]:
    """把候选标的拆成 skip / fetch 两组（纯函数，便于 dry-run 与单测）。"""
    skip: List[str] = []
    fetch: List[str] = []
    seen = set()
    for raw in codes:
        code = str(raw or '').strip()
        if not code or code in seen:
            continue
        seen.add(code)
        if not force and int(bar_counts.get(code, 0) or 0) >= int(min_bars):
            skip.append(code)
        else:
            fetch.append(code)
    if limit and int(limit) > 0:
        fetch = fetch[:int(limit)]
    return {'fetch': fetch, 'skip': skip}


def _is_supported_code(code: str) -> bool:
    """跳过 board_* 等非交易标的；六位代码（含 ETF）才回填。"""
    code = str(code or '').strip()
    return len(code) == 6 and code.isdigit()


# ---------------------------------------------------------------------------
# 代码分类与范围过滤（纯函数，便于单测）
# ---------------------------------------------------------------------------
_ETF_SH_PREFIX = ('51', '52', '56', '58', '530', '551')
_ETF_SZ_PREFIX = ('158', '159')
_STAR_PREFIX = ('688', '689')
_GEM_PREFIX = ('300', '301', '302')
_BSE_PREFIX = ('43', '83', '87', '92')
_MAIN_SH_PREFIX = ('600', '601', '603', '605')
_MAIN_SZ_PREFIX = ('000', '001', '002', '003')


@dataclass(frozen=True)
class CodeMeta:
    code: str
    kind: str    # stock | etf
    market: str  # sh | sz | bj
    board: str   # main | gem | star | bse | etf


def is_etf_code(code: str) -> bool:
    """场内 ETF 判定（与 HithinkDataSource._is_etf 同口径；LOF/REITs 不算）。"""
    c = str(code or '').strip()
    if len(c) != 6 or not c.isdigit():
        return False
    if c.startswith(_ETF_SH_PREFIX):
        return True
    return c.startswith(_ETF_SZ_PREFIX)


def classify_code(code: str) -> Optional[CodeMeta]:
    """6 位代码 → 元信息；非 6 位数字一律 None（板块/指数代码不在此判定）。"""
    c = str(code or '').strip()
    if len(c) != 6 or not c.isdigit():
        return None
    if is_etf_code(c):
        market = 'sh' if c.startswith(_ETF_SH_PREFIX) else 'sz'
        return CodeMeta(c, 'etf', market, 'etf')
    if c.startswith(_STAR_PREFIX):
        return CodeMeta(c, 'stock', 'sh', 'star')
    if c.startswith(_MAIN_SH_PREFIX):
        return CodeMeta(c, 'stock', 'sh', 'main')
    if c.startswith(_GEM_PREFIX):
        return CodeMeta(c, 'stock', 'sz', 'gem')
    if c.startswith(_MAIN_SZ_PREFIX):
        return CodeMeta(c, 'stock', 'sz', 'main')
    if c.startswith(_BSE_PREFIX):
        return CodeMeta(c, 'stock', 'bj', 'bse')
    return None


def filter_codes(codes: Iterable[str], *, markets=None, boards=None,
                 kinds=None) -> List[str]:
    """按市场/板块/类型过滤，去重且保持输入顺序。"""
    ms, bs, ks = set(markets or ()), set(boards or ()), set(kinds or ())
    out: List[str] = []
    seen = set()
    for raw in codes:
        meta = classify_code(str(raw))
        if meta is None or meta.code in seen:
            continue
        if ms and meta.market not in ms:
            continue
        if bs and meta.board not in bs:
            continue
        if ks and meta.kind not in ks:
            continue
        seen.add(meta.code)
        out.append(meta.code)
    return out


# ---------------------------------------------------------------------------
# 缺口判定与范围解析（--stale-only / --scope watchlist）
# ---------------------------------------------------------------------------
def plan_stale(codes: Iterable[str], latest: Dict[str, str], *, target: str,
               force: bool = False, limit: int = 0) -> Dict[str, List[str]]:
    """按「最新 bar 日期 < target」判缺口；latest 缺失视为冷启动（要拉）。"""
    fetch: List[str] = []
    skip: List[str] = []
    seen = set()
    for raw in codes:
        code = str(raw or '').strip()
        if not code or code in seen:
            continue
        seen.add(code)
        if not force and str(latest.get(code) or '') >= str(target):
            skip.append(code)
        else:
            fetch.append(code)
    if limit and int(limit) > 0:
        fetch = fetch[:int(limit)]
    return {'fetch': fetch, 'skip': skip}


def load_watchlist(radar_db: Optional[Path] = None) -> List[str]:
    """持仓（qty>0）∪ 自选；库/表缺失时返回空列表，不抛。"""
    import sqlite3
    path = Path(radar_db) if radar_db else (ROOT / 'db' / 'stock_radar.db')
    if not path.exists():
        return []
    codes = set()
    con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    try:
        for sql in ('SELECT stock_code FROM watchlist',
                    'SELECT stock_code FROM portfolio_position WHERE COALESCE(qty, 0) > 0'):
            try:
                codes.update(str(r[0]) for r in con.execute(sql) if r and r[0])
            except sqlite3.Error:
                continue
    finally:
        con.close()
    return sorted(c for c in codes if classify_code(c))


def _market_db_ctx():
    from utils.cache.market_data_db import get_market_data_db
    return get_market_data_db()


def latest_dates(codes: Iterable[str], batch: int = 500) -> Dict[str, str]:
    """批量读出每只标的的最新 bar 日期；只读，失败返回已读到的部分。"""
    code_list = [str(c) for c in codes if c]
    out: Dict[str, str] = {}
    if not code_list:
        return out
    with _market_db_ctx() as conn:
        for i in range(0, len(code_list), max(1, int(batch))):
            chunk = code_list[i:i + int(batch)]
            marks = ','.join('?' * len(chunk))
            rows = conn.execute(
                f'SELECT code, MAX(trade_date) FROM stock_daily '
                f'WHERE code IN ({marks}) GROUP BY code', chunk).fetchall()
            for r in rows:
                out[str(r[0])] = str(r[1] or '')
    return out


def _split_csv(value: str) -> List[str]:
    return [x.strip() for x in str(value or '').split(',') if x.strip()]


def resolve_codes(*, scope: str, kinds, markets, boards) -> List[str]:
    """scope → 候选代码，再按 market/board/kind 过滤。"""
    base = load_watchlist() if scope == 'watchlist' else _stock_codes(include_etf=True)
    return filter_codes(base, markets=markets, boards=boards, kinds=kinds)


def pick_source(code: str, source: str) -> str:
    """ETF 在 auto 下显式走 hithink 基金端点，避免按基础优先级落到 yfinance。"""
    src = (source or 'auto').strip() or 'auto'
    if src == 'auto' and is_etf_code(code):
        return 'hithink'
    return src


def load_index_items(market_db: Optional[Path] = None) -> List[Tuple[str, str]]:
    """index_info 全量 (code, name)；库缺失返回空列表。"""
    import sqlite3
    path = Path(market_db) if market_db else (ROOT / 'db' / 'market_data.db')
    if not path.exists():
        return []
    con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT code, name FROM index_info').fetchall()
    finally:
        con.close()
    return [(str(c), str(n or '')) for c, n in rows]


# ---------------------------------------------------------------------------
# DB / 任务辅助
# ---------------------------------------------------------------------------
def _db_ready() -> bool:
    from utils.cache.market_data_db import get_market_data_db
    try:
        with get_market_data_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_daily'"
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _ensure_db() -> None:
    if _db_ready():
        return
    import subprocess
    print('[BOOTSTRAP] market_data.db 缺 stock_daily，先跑 init_dbs.py --db market_data')
    subprocess.run([sys.executable, str(ROOT / 'scripts/ops/init_dbs.py'),
                    '--db', 'market_data'], check=True)


def _stock_codes(include_etf: bool = True) -> List[str]:
    from utils.cache.market_data_db import get_market_data_db
    with get_market_data_db() as conn:
        rows = conn.execute('SELECT code FROM stock_info ORDER BY code').fetchall()
    codes = [str(r['code'] if isinstance(r, dict) or hasattr(r, 'keys') else r[0])
             for r in rows]
    if not include_etf:
        # ETF 前缀与个股不同；stock_info.market='ETF' 更准，但这里只按代码保守过滤
        codes = [c for c in codes if not c.startswith(('51', '52', '56', '58', '15', '16', '18'))]
    return [c for c in codes if _is_supported_code(c)]


def _bar_counts(codes: List[str], start: str, end: str) -> Dict[str, int]:
    from utils.cache.market_data_db import get_market_data_db
    if not codes:
        return {}
    out: Dict[str, int] = {}
    with get_market_data_db() as conn:
        # 分批 IN，避免 SQLite 变量上限
        for i in range(0, len(codes), 400):
            chunk = codes[i:i + 400]
            marks = ','.join('?' * len(chunk))
            rows = conn.execute(
                f'SELECT code, COUNT(*) AS n FROM stock_daily '
                f'WHERE code IN ({marks}) AND trade_date >= ? AND trade_date <= ? '
                f'GROUP BY code', (*chunk, start, end)).fetchall()
            for r in rows:
                out[str(r['code'])] = int(r['n'])
    return out


def _run_task(task_type: str, config: dict) -> None:
    """同步执行一个 DataCollector 任务（股票/ETF 清单、指数、板块）。"""
    from backtest.data_collector import DataCollector
    from utils.cache.backtest_db import create_data_task

    task_id = f'BOOT-{datetime.now().strftime("%Y%m%d%H%M%S")}-{task_type}'
    create_data_task(task_id, task_type, json.dumps(config, ensure_ascii=False))
    DataCollector()._execute_task(task_id, task_type, config)


def _try_bulk_dump(kind: str, progress_cb=None, start_date: str = '') -> bool:
    """有 key 时走同花顺全市场 Parquet；没有则返回 False（调用方回落逐只）。"""
    if not os.getenv('HITHINK_FINANCE_API_KEY'):
        return False
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        print('[BOOTSTRAP] 未安装 pyarrow，跳过全市场 dump（pip install pyarrow 后可启用）')
        return False
    from backtest.hithink_marketdb import sync_dump_stock_daily
    print(f'[BOOTSTRAP] 走全市场 Parquet dump: {kind}')
    if kind == 'daily-k':
        print('[BOOTSTRAP] 全量 daily-k 约 945 万行，首次下载+灌库可能数分钟到十几分钟；'
              '只补最近 10 个交易日请用 --dump-kind daily-k-10d')
        if start_date:
            print(f'[BOOTSTRAP] 入库按 --since {start_date} 过滤（下载仍是全量 Parquet）')
    n = sync_dump_stock_daily(kind, progress_cb=progress_cb, start_date=start_date)
    print(f'[BOOTSTRAP] dump 完成: {n} 行')
    return True


def _fetch_one(code: str, days: int, retries: int, sleep: float,
               source: str = 'auto') -> Dict[str, Any]:
    from utils.cache.market_data_db import fetch_and_store_daily
    src = pick_source(code, source)
    last = ''
    for attempt in range(max(1, int(retries) + 1)):
        try:
            n = fetch_and_store_daily(code, days=days, source=src)
            if sleep > 0:
                time.sleep(sleep)
            return {'code': code, 'ok': True, 'rows': int(n or 0), 'attempts': attempt + 1}
        except Exception as e:  # noqa: BLE001 - 单只失败不拖垮整批
            last = str(e)[:200]
            # 新上市标的本地零行时，大窗口会被「K 线过少」闸门拒收
            # （min_rows = days//5，1071 天要 214 根）。用 120 天窗口重试一次。
            if attempt == 0 and '过少' in last and int(days) > 120:
                try:
                    n = fetch_and_store_daily(code, days=120, source=src)
                    if sleep > 0:
                        time.sleep(sleep)
                    return {'code': code, 'ok': True, 'rows': int(n or 0),
                            'attempts': 1, 'window': 120}
                except Exception as e2:  # noqa: BLE001
                    last = str(e2)[:200]
            if attempt < retries:
                time.sleep(max(0.5, sleep) * (attempt + 1))
    return {'code': code, 'ok': False, 'error': last}


# ---------------------------------------------------------------------------
# 状态 / 报告
# ---------------------------------------------------------------------------
def _write_status(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _fmt_eta(done: int, total: int, elapsed: float) -> str:
    if done <= 0 or total <= 0:
        return '?'
    per = elapsed / done
    left = max(0, total - done) * per
    return f'{left / 60:.1f} 分钟'


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='新环境行情数据回填总入口（只补 market_data.db）')
    p.add_argument('--mode', choices=['auto', 'dump', 'per-code'], default='auto',
                   help='auto=有 key 先 dump 再补缺口；dump=只用全市场 Parquet；per-code=逐只')
    p.add_argument('--dump-kind', choices=['daily-k', 'daily-k-10d'], default='daily-k',
                   help='全市场 dump 类型；daily-k 约 10 年')
    p.add_argument('--since', default='2024-01-01',
                   help='只入库该日期之后的数据（默认 2024-01-01；传空串则用 --years）')
    p.add_argument('--years', type=float, default=DEFAULT_YEARS,
                   help='--since 为空时的回填年数（默认 2）')
    p.add_argument('--min-bars', type=int, default=DEFAULT_MIN_BARS,
                   help='窗口内已有 >=N 根就跳过（默认 250）')
    p.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                   help='逐只并发数；SQLite 写入建议 1~2（默认 1）')
    p.add_argument('--sleep', type=float, default=DEFAULT_SLEEP,
                   help='逐只之间的休眠秒数（默认 0.2）')
    p.add_argument('--retries', type=int, default=DEFAULT_RETRIES, help='单只重试次数')
    p.add_argument('--source', default='auto',
                   help='逐只取数源：auto / akshare / hithink（默认 auto）')
    p.add_argument('--codes', default='', help='只回填这些代码（逗号分隔）')
    p.add_argument('--limit', type=int, default=0, help='只跑前 N 只（烟测用）')
    p.add_argument('--force', action='store_true', help='已有足够根数也重拉')
    p.add_argument('--dry-run', action='store_true', help='只打印计划，不联网/不写库')
    p.add_argument('--skip-lists', action='store_true', help='跳过 stock_list/etf_list/index_kline')
    p.add_argument('--with-boards', action='store_true', help='同时补板块列表/成员/K线')
    p.add_argument('--fill-gaps', action='store_true', help='dump 后仍对 <min-bars 的标的逐只补')
    p.add_argument('--audit', action='store_true', help='结束后跑 audit_market_coverage.py')
    p.add_argument('--status-file', default=str(DEFAULT_STATUS), help='进度 JSON 路径')
    p.add_argument('--no-etf', action='store_true', help='不把 ETF 纳入逐只回填')
    p.add_argument('--scope', choices=['all', 'watchlist'], default='all',
                   help='all=全量清单（默认，保持新环境语义）；watchlist=持仓(qty>0)∪自选')
    p.add_argument('--stale-only', action='store_true',
                   help='按「最新 bar < 目标交易日」只补落后/冷启动标的（推荐日常使用）')
    p.add_argument('--type', default='stock,etf',
                   help='stock,etf,index,board 逗号组合（index/board 走既有任务）')
    p.add_argument('--market', default='', help='sh,sz,bj 逗号组合（仅 stock/etf）')
    p.add_argument('--board', default='', help='main,gem,star,bse 逗号组合')
    p.add_argument('--target-date', default='', help='缺口基准日 YYYY-MM-DD（默认最近已完成交易日）')
    return p


def _resolve_plan(args, start: str, end: str):
    """scope/type/market/board → (候选代码, fetch/skip 计划)。"""
    types = _split_csv(args.type)
    kinds = {'stock', 'etf'} & set(types)
    if args.no_etf:
        kinds.discard('etf')
    if args.codes:
        codes = [c.strip() for c in str(args.codes).split(',') if c.strip()]
    else:
        codes = resolve_codes(scope=args.scope, kinds=kinds or None,
                              markets=set(_split_csv(args.market)) or None,
                              boards=set(_split_csv(args.board)) or None)
    if args.stale_only:
        from utils.trading_calendar import get_effective_trading_date
        target = args.target_date or get_effective_trading_date('cn').isoformat()
        plan = plan_stale(codes, latest_dates(codes), target=target,
                          force=args.force, limit=args.limit)
    else:
        counts = _bar_counts(codes, start, end)
        plan = plan_codes(codes, counts, min_bars=args.min_bars,
                          force=args.force, limit=args.limit)
    return codes, plan


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.target_date:
        try:
            date.fromisoformat(str(args.target_date).strip()[:10])
        except ValueError:
            print(f'[BOOTSTRAP] --target-date 不是合法日期: {args.target_date}')
            return 2
    end = date.today().isoformat()
    if args.since:
        try:
            since = date.fromisoformat(str(args.since).strip()[:10])
        except ValueError:
            print(f'[BOOTSTRAP] --since 不是合法日期: {args.since}')
            return 2
        days = min(2000, max(60, (date.today() - since).days + 30))
        start = since.isoformat()
    else:
        days = lookback_days(args.years, args.min_bars)
        start = window_start(days)
    status_path = Path(args.status_file)
    status: Dict[str, Any] = {
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'args': vars(args), 'window': {'start': start, 'end': end, 'days': days},
        'phase': 'init', 'done': 0, 'total': 0, 'ok': 0, 'failed': 0,
        'skipped': 0, 'failed_codes': [],
    }
    _write_status(status_path, status)

    print(f'[BOOTSTRAP] 窗口 {start} ~ {end}（{days} 天），min_bars={args.min_bars}')
    if args.dry_run:
        if not _db_ready():
            print('[DRY-RUN] market_data.db 还没有 stock_daily；'
                  '先跑 python scripts/ops/init_dbs.py --db market_data')
            return 1
        codes, plan = _resolve_plan(args, start, end)
        mode_desc = 'stale-only' if args.stale_only else f'min_bars={args.min_bars}'
        print(f'[DRY-RUN] scope={args.scope} type={args.type} 判缺口={mode_desc}')
        print(f'[DRY-RUN] 候选 {len(codes)} 只；需回填 {len(plan["fetch"])} 只；'
              f'已足够跳过 {len(plan["skip"])} 只')
        print('[DRY-RUN] 前 10 只需回填:', plan['fetch'][:10])
        if args.codes:
            print('[DRY-RUN] 指定代码计划:', plan)
        status.update({'phase': 'dry-run', 'total': len(plan['fetch']),
                       'skipped': len(plan['skip'])})
        _write_status(status_path, status)
        return 0

    _ensure_db()

    # 1) 清单 / 指数 / 板块
    types = _split_csv(args.type)
    if 'board' in types:
        args.with_boards = True
    if not args.skip_lists:
        status['phase'] = 'lists'
        _write_status(status_path, status)
        if args.scope != 'watchlist':
            for task in ('stock_list', 'etf_list'):
                try:
                    print(f'[BOOTSTRAP] 执行 {task} ...')
                    _run_task(task, {})
                except Exception as e:  # noqa: BLE001 - 清单失败不阻断逐只回填
                    print(f'[BOOTSTRAP] {task} 失败（继续）: {e}')
        if args.scope != 'watchlist' or 'index' in types:
            index_cfg = {'days': max(500, min(2000, days))}
            if 'index' in types:
                indices = load_index_items()
                if indices:
                    index_cfg['indices'] = indices
                    print(f'[BOOTSTRAP] 指数清单 {len(indices)} 只（来自 index_info）')
            try:
                print('[BOOTSTRAP] 执行 index_kline ...')
                _run_task('index_kline', index_cfg)
            except Exception as e:  # noqa: BLE001
                print(f'[BOOTSTRAP] index_kline 失败（继续）: {e}')
        if args.with_boards:
            for task, cfg in (('board_list', {}),
                              ('board_member', {'board_type': 'industry'}),
                              ('board_kline', {'board_type': 'industry'})):
                try:
                    print(f'[BOOTSTRAP] 执行 {task} ...')
                    _run_task(task, cfg)
                except Exception as e:  # noqa: BLE001
                    print(f'[BOOTSTRAP] {task} 失败（继续）: {e}')

    # 2) 全市场 dump（有 key 时）
    # ⚠️ --codes / --limit 是「只补这几只」的意图，绝不能触发全市场 945 万行下载。
    # ⚠️ --scope watchlist 同理：几十只标的绝不能触发全市场 dump。
    _explicit_subset = bool(args.codes) or int(args.limit or 0) > 0
    if args.scope == 'watchlist' and args.mode == 'dump':
        print('[BOOTSTRAP] --scope watchlist 不支持 --mode dump（批量接口无法只取几十只），'
              '请改用 --mode per-code 或去掉 --mode')
        return 2
    if _explicit_subset and args.mode == 'dump':
        print('[BOOTSTRAP] mode=dump 与 --codes/--limit 冲突：批量 dump 不支持指定标的，'
              '请改用 --mode per-code')
        return 2
    bulk_done = False
    if args.mode in ('auto', 'dump') and not _explicit_subset and args.scope != 'watchlist':
        status['phase'] = 'dump'
        _write_status(status_path, status)
        try:
            bulk_done = _try_bulk_dump(args.dump_kind, start_date=args.since)
        except Exception as e:  # noqa: BLE001
            if args.mode == 'dump':
                print(f'[BOOTSTRAP] dump 失败: {e}')
                return 2
            print(f'[BOOTSTRAP] dump 失败，回落逐只: {e}')
    if args.mode == 'dump' and not bulk_done:
        print('[BOOTSTRAP] mode=dump 但 HITHINK_FINANCE_API_KEY 未配置或 pyarrow 缺失')
        return 2

    # 3) 逐只回填（无 key 的默认路径；或 --fill-gaps）
    if bulk_done and not args.fill_gaps:
        print('[BOOTSTRAP] dump 已完成；未指定 --fill-gaps，跳过逐只')
    else:
        status['phase'] = 'per-code'
        _write_status(status_path, status)
        codes, plan = _resolve_plan(args, start, end)
        fetch, skipped = plan['fetch'], plan['skip']
        status.update({'total': len(fetch), 'skipped': len(skipped)})
        _write_status(status_path, status)
        print(f'[BOOTSTRAP] 需回填 {len(fetch)} 只，跳过 {len(skipped)} 只，'
              f'workers={max(1, args.workers)}')
        if not fetch:
            print('[BOOTSTRAP] 没有需要回填的标的')
        else:
            t0 = time.time()
            failed_codes: List[str] = []
            ok = 0
            workers = max(1, min(int(args.workers), 4))
            if workers == 1:
                results = []
                for i, code in enumerate(fetch, 1):
                    results.append(_fetch_one(code, days, args.retries, args.sleep,
                                              source=args.source))
                    _tick(status_path, status, i, len(fetch), results[-1], t0)
                    if not results[-1]['ok']:
                        failed_codes.append(code)
            else:
                results = []
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(_fetch_one, c, days, args.retries, args.sleep,
                                        args.source): c
                            for c in fetch}
                    for i, fut in enumerate(as_completed(futs), 1):
                        r = fut.result()
                        results.append(r)
                        if not r['ok']:
                            failed_codes.append(r['code'])
                        _tick(status_path, status, i, len(fetch), r, t0)
            ok = sum(1 for r in results if r.get('ok'))
            status.update({'phase': 'done', 'ok': ok,
                           'failed': len(failed_codes), 'failed_codes': failed_codes[:500],
                           'done': len(fetch), 'elapsed_sec': round(time.time() - t0, 1)})
            _write_status(status_path, status)
            print(f'[BOOTSTRAP] 逐只完成：成功 {ok}，失败 {len(failed_codes)}；'
                  f'状态 {status_path}')

    # 4) 审计
    if args.audit:
        status['phase'] = 'audit'
        _write_status(status_path, status)
        import subprocess
        cmd = [sys.executable, str(ROOT / 'scripts/ops/audit_market_coverage.py'),
               '--start', start, '--end', end, '--top', '10']
        print('[BOOTSTRAP] 跑覆盖审计 ...')
        try:
            subprocess.run(cmd, check=False)
        except Exception as e:  # noqa: BLE001
            print(f'[BOOTSTRAP] 审计失败（不影响回填结果）: {e}')

    status['phase'] = 'finished'
    status['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _write_status(status_path, status)
    return 0


def _tick(path: Path, status: Dict[str, Any], i: int, total: int,
          result: Dict[str, Any], t0: float) -> None:
    status['done'] = i
    if result.get('ok'):
        status['ok'] = int(status.get('ok', 0)) + 1
    else:
        status['failed'] = int(status.get('failed', 0)) + 1
        status.setdefault('failed_codes', []).append(result.get('code'))
        status['failed_codes'] = status['failed_codes'][:500]
    if i % 10 == 0 or i == total:
        elapsed = max(0.0, time.time() - t0)
        print(f'[BOOTSTRAP] {i}/{total} ok={status["ok"]} fail={status["failed"]} '
              f'ETA {_fmt_eta(i, total, elapsed)}')
    if i % 20 == 0 or i == total:
        status['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _write_status(path, status)


if __name__ == '__main__':
    raise SystemExit(main())
