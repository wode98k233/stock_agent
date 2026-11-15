"""「换个标的验证」候选池（Plan 3 任务 8 / 设计 §10）。

只读本地库，不联网、不写库：
- market_data.db：stock_board / stock_board_member（行业归属）+ stock_info（成员名、ST 标记）
- stock_radar.db：portfolio_position（持仓）∪ watchlist（自选）作为行业不可用时的回退池

行业口径与 signal_eval/benchmarks.py::_load_mapping_from_db 保持一致：只认
board_type='industry' AND is_active=1，同一只票属多个行业板块时取成分数最少的
（最专一）——否则「制造业」这类超大板块会把候选池稀释成一堆不相关标的，
外部验证就失去意义了（换一只不相关的票回测，结论无法说明原策略是否过拟合）。
"""
import logging
import re
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r'^(sh|sz|bj)', re.IGNORECASE)
_BARE_RE = re.compile(r'^\d{6}$')


def _bare(code: str) -> str:
    """剥掉 sh/sz/bj 前缀，返回裸 6 位代码；非法返回 ''。"""
    text = str(code or '').strip().lower()
    text = _PREFIX_RE.sub('', text)
    return text if _BARE_RE.fullmatch(text) else ''


def _industry_boards_of(bare: str) -> List[Dict[str, str]]:
    """查该标的所属行业板块，按成分数从少到多排序（最专一在前）；查不到返回 []。"""
    code = _bare(bare)
    if not code:
        return []
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            # 成员表里 code 可能带前缀（sh600519）也可能不带，四种写法一起试
            rows = conn.execute('''
                SELECT b.board_id, b.board_name
                FROM stock_board_member m
                JOIN stock_board b ON b.board_id = m.board_id
                WHERE b.board_type = 'industry' AND b.is_active = 1
                  AND m.code IN (?, ?, ?, ?)
            ''', (code, f'sh{code}', f'sz{code}', f'bj{code}')).fetchall()
            if not rows:
                return []
            counts = dict(conn.execute(
                'SELECT board_id, COUNT(*) FROM stock_board_member GROUP BY board_id'
            ).fetchall())
    except sqlite3.Error as e:
        # 旧库缺表/缺列：降级到持仓自选池，不影响「换个标的验证」主流程
        logger.debug(f'[peer] 行业板块读取失败，降级到持仓自选: {e}')
        return []
    rows.sort(key=lambda r: (counts.get(r['board_id'], 0), r['board_id']))
    return [{'board_id': r['board_id'], 'board_name': r['board_name'] or ''} for r in rows]


def _peers_in_board(board_id: str, exclude: str, limit: int,
                    include_st: bool = False) -> List[Dict[str, str]]:
    """同板块成员（带名字），默认剔除 ST；返回最多 limit 只。"""
    if not board_id or limit <= 0:
        return []
    exclude = _bare(exclude)
    try:
        from utils.cache.market_data_db import get_market_data_db
        with get_market_data_db() as conn:
            # 多取 4 倍：ST 过滤 + 前缀/重复剔除之后仍要凑够 limit
            rows = conn.execute('''
                SELECT DISTINCT m.code AS code, s.name AS name, s.is_st AS is_st
                FROM stock_board_member m
                LEFT JOIN stock_info s ON s.code = m.code
                WHERE m.board_id = ?
                ORDER BY m.code
                LIMIT ?
            ''', (board_id, max(limit * 4, 12))).fetchall()
    except sqlite3.Error as e:
        logger.debug(f'[peer] 板块成员读取失败: {e}')
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for r in rows:
        code = _bare(r['code'])
        if not code or code in seen or code == exclude:
            continue
        if not include_st and int(r['is_st'] or 0):
            continue
        seen.add(code)
        out.append({'code': code, 'name': r['name'] or ''})
        if len(out) >= limit:
            break
    return out


def _holdings_watchlist(limit: int) -> List[Dict[str, str]]:
    """回退池：持仓（qty>0）∪ 自选（仅 A 股），去重保序，最多 limit 只。

    与 backtest/data_collector.py::_load_holding_watch_codes 同口径（持仓在前）。
    """
    if limit <= 0:
        return []
    from utils.app_paths import get_db_path
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            rows = conn.execute(
                "SELECT stock_code FROM portfolio_position "
                "WHERE qty > 0 AND market = 'cn' ORDER BY rowid").fetchall()
            rows += conn.execute(
                "SELECT stock_code FROM watchlist "
                "WHERE market = 'cn' ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug(f'[peer] 持仓/自选读取失败: {e}')
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        code = _bare(row[0])
        if not code or code in seen:
            continue
        seen.add(code)
        # 名字留空：调用方（前端）拿 code 就够用，避免为回退池多查一次行情库
        out.append({'code': code, 'name': ''})
        if len(out) >= limit:
            break
    return out


def resolve_peer_candidates(code: str, limit: int = 3,
                            include_st: bool = False) -> Dict[str, Any]:
    """给出「换哪几只票」的候选：同行业优先，行业不可用时回退持仓 ∪ 自选。

    Returns:
        {
          'code': 原标的,
          'source': 'industry' | 'portfolio_watchlist' | 'none',
          'board_name': 命中的行业板块名（无则空串）,
          'candidates': [{'code', 'name'}, ...],
          'hint': 面向用户的一句话说明（无候选时说明为什么）。
        }
    """
    limit = max(1, int(limit or 3))
    bare = _bare(code)
    result: Dict[str, Any] = {
        'code': str(code or ''),
        'source': 'none',
        'board_name': '',
        'candidates': [],
        'hint': '',
    }
    if not bare:
        result['hint'] = f'标的代码无法识别：{code}'
        return result

    # 一个标的可能挂多个行业板块：从最专一的开始试，试到某块里有别的成分股为止
    # （「只有自己」的小板块作候选池没有意义，但也别因此丢掉更大的同行板块）
    boards = _industry_boards_of(bare)
    for board in boards[:3]:
        peers = _peers_in_board(board['board_id'], bare, limit, include_st=include_st)
        if peers:
            result.update({
                'source': 'industry',
                'board_name': board['board_name'],
                'candidates': peers,
                'hint': f"同行业板块「{board['board_name']}」里的 "
                        f"{len(peers)} 只候选" + ('' if include_st else '（已剔除 ST）'),
            })
            return result
    if boards:
        # 命中了行业板块、但块里没有可换的同行：把板块名留下来供提示用
        result['board_name'] = boards[0]['board_name']

    others = [p for p in _holdings_watchlist(limit + 1) if p['code'] != bare][:limit]
    if others:
        result.update({
            'source': 'portfolio_watchlist',
            'candidates': others,
            'hint': '没找到可用的同行业归属，改用「持仓 ∪ 自选」里的标的做外部验证',
        })
        return result

    result['hint'] = ('既没有同行业候选，持仓/自选里也没有可换的标的'
                      '（先加自选股或建仓，再回来换个标的验证）')
    return result
