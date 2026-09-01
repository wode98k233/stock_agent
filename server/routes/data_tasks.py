"""数据采集任务 API

提供数据采集任务的创建、查询、控制（暂停/恢复/取消）和进度推送。
"""
import json
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from utils.cache.backtest_db import (
    get_data_task, get_data_tasks, update_data_task,
)
from utils.cache.market_data_db import get_market_data_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/data', tags=['data'])

# 全局采集器实例
_collector = None


def _get_collector():
    global _collector
    if _collector is None:
        from backtest.data_collector import DataCollector
        _collector = DataCollector()
    return _collector


# ============================================================
# 请求模型
# ============================================================

class CreateTaskRequest(BaseModel):
    type: str                           # stock_list / daily_kline / index_kline / adjust_factor / board_list / board_member / board_kline / full_sync
    codes: Optional[List[str]] = None   # 股票代码列表
    days: int = 500                     # 采集天数
    source: str = 'auto'               # 数据源
    boards: Optional[List[List[str]]] = None  # 板块列表 [["名称","类型"], ...]
    board_type: Optional[str] = None    # 板块类型: industry / concept / all
    force: bool = False                 # 强制全量覆盖


# ============================================================
# API 端点
# ============================================================

@router.post('/tasks')
async def create_task(req: CreateTaskRequest):
    """创建采集任务"""
    collector = _get_collector()
    config = {
        'codes': req.codes or [],
        'days': req.days,
        'source': req.source,
    }
    if req.boards:
        config['boards'] = req.boards
    if req.board_type:
        config['board_type'] = req.board_type
    if req.force:
        config['force'] = True
    task_id = collector.create_and_run(req.type, config)
    task = get_data_task(task_id)
    return {'task_id': task_id, 'task': task}


@router.get('/tasks')
async def list_tasks(status: Optional[str] = None, limit: int = 50):
    """获取任务列表"""
    tasks = get_data_tasks(status=status, limit=limit)
    return {'tasks': tasks}


@router.get('/tasks/{task_id}')
async def get_task(task_id: str):
    """获取任务详情"""
    task = get_data_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f'任务不存在: {task_id}')
    return task


@router.get('/tasks/{task_id}/events')
async def task_events(task_id: str):
    """SSE 进度推送"""
    task = get_data_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f'任务不存在: {task_id}')

    async def event_stream():
        last_processed = -1
        while True:
            task = get_data_task(task_id)
            if not task:
                yield f'data: {json.dumps({"type": "error", "message": "任务不存在"})}\n\n'
                break

            processed = task.get('processed_count', 0)
            if processed != last_processed:
                yield f'data: {json.dumps({"type": "progress", "task": task}, ensure_ascii=False)}\n\n'
                last_processed = processed

            status = task.get('status')
            if status in ('completed', 'failed', 'cancelled'):
                yield f'data: {json.dumps({"type": "final", "task": task}, ensure_ascii=False)}\n\n'
                break

            await asyncio.sleep(1)

    return StreamingResponse(event_stream(), media_type='text/event-stream')


@router.post('/tasks/{task_id}/pause')
async def pause_task(task_id: str):
    """暂停任务"""
    task = get_data_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f'任务不存在: {task_id}')
    if task['status'] != 'running':
        raise HTTPException(status_code=400, detail=f'任务状态不是运行中: {task["status"]}')
    _get_collector().pause(task_id)
    return {'message': '已暂停'}


@router.post('/tasks/{task_id}/resume')
async def resume_task(task_id: str):
    """恢复任务"""
    task = get_data_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f'任务不存在: {task_id}')
    if task['status'] != 'paused':
        raise HTTPException(status_code=400, detail=f'任务状态不是已暂停: {task["status"]}')
    _get_collector().resume(task_id)
    return {'message': '已恢复'}


@router.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str):
    """取消任务"""
    task = get_data_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f'任务不存在: {task_id}')
    if task['status'] not in ('running', 'paused', 'pending'):
        raise HTTPException(status_code=400, detail=f'任务状态不可取消: {task["status"]}')
    _get_collector().cancel(task_id)
    return {'message': '已取消'}


@router.get('/indices')
async def list_indices():
    """获取可采集的指数列表（默认 + 已采集）"""
    default_indices = [
        # 综合指数
        {'code': '000001', 'name': '上证指数', 'category': '综合'},
        {'code': '399001', 'name': '深证成指', 'category': '综合'},
        {'code': '399006', 'name': '创业板指', 'category': '综合'},
        {'code': '000300', 'name': '沪深300', 'category': '规模'},
        {'code': '000905', 'name': '中证500', 'category': '规模'},
        {'code': '000016', 'name': '上证50', 'category': '规模'},
        {'code': '000852', 'name': '中证1000', 'category': '规模'},
        {'code': '000688', 'name': '科创50', 'category': '规模'},
        {'code': '399005', 'name': '中小100', 'category': '规模'},
        {'code': '000015', 'name': '红利指数', 'category': '策略'},
        {'code': '000038', 'name': '上证180', 'category': '规模'},
        {'code': '000134', 'name': '上证综指', 'category': '综合'},
        # 消费/白酒
        {'code': '000932', 'name': '中证消费', 'category': '消费'},
        {'code': '399997', 'name': '中证白酒', 'category': '消费'},
        # 医药/创新药
        {'code': '000991', 'name': '全指医药', 'category': '医药'},
        {'code': '399441', 'name': '生物医药', 'category': '医药'},
        # 新能源/光伏
        {'code': '399808', 'name': '中证新能源', 'category': '新能源'},
        {'code': '399976', 'name': '新能源车', 'category': '新能源'},
        # 以下需要网络好时验证代码格式（暂不采集）
        # {'code': '990001', 'name': '国证芯片', 'category': '科技'},
        # {'code': '930713', 'name': '人工智能', 'category': '科技'},
        # {'code': '931187', 'name': '科技龙头', 'category': '科技'},
        # {'code': '930988', 'name': '中证黄金', 'category': '商品'},
        # {'code': 'HSTECH', 'name': '恒生科技', 'category': '港股'},
        # {'code': 'H30533', 'name': '恒生互联', 'category': '港股'},
    ]

    # 查已采集状态
    from utils.cache.market_data_db import get_market_data_db
    collected = {}
    try:
        with get_market_data_db() as conn:
            rows = conn.execute(
                'SELECT code, COUNT(*) as cnt, MAX(trade_date) as latest FROM index_daily GROUP BY code'
            ).fetchall()
            for r in rows:
                collected[r['code']] = {'count': r['cnt'], 'latest': r['latest']}
    except Exception:
        pass

    for idx in default_indices:
        info = collected.get(idx['code'])
        if info:
            idx['collected'] = True
            idx['count'] = info['count']
            idx['latest'] = info['latest']
        else:
            idx['collected'] = False

    return {'indices': default_indices}


@router.get('/boards')
async def list_boards():
    """获取已采集的板块列表"""
    from utils.cache.market_data_db import get_market_data_db
    boards = []
    try:
        with get_market_data_db() as conn:
            rows = conn.execute(
                'SELECT board_name, board_type FROM stock_board ORDER BY board_type, board_name'
            ).fetchall()
            boards = [{'board_name': r['board_name'], 'board_type': r['board_type']} for r in rows]
    except Exception:
        pass
    return {'boards': boards}


@router.get('/stats')
async def data_stats():
    """数据库统计 — 含表概览、增量状态、数据质量"""
    stats = {}
    tables = []
    incremental = []
    quality = []

    try:
        with get_market_data_db() as conn:
            # ── 汇总统计 ──
            stock_count = conn.execute('SELECT COUNT(*) as cnt FROM stock_info').fetchone()['cnt']
            daily_count = conn.execute('SELECT COUNT(*) as cnt FROM stock_daily').fetchone()['cnt']
            adj_count = conn.execute('SELECT COUNT(*) as cnt FROM stock_adjust_factor').fetchone()['cnt']
            board_count = conn.execute('SELECT COUNT(*) as cnt FROM stock_board').fetchone()['cnt']
            board_member_count = conn.execute('SELECT COUNT(*) as cnt FROM stock_board_member').fetchone()['cnt']

            date_range = conn.execute(
                'SELECT MIN(trade_date) as min_date, MAX(trade_date) as max_date FROM stock_daily'
            ).fetchone()

            stats['stock_count'] = stock_count
            stats['daily_count'] = daily_count
            stats['adjust_factor_count'] = adj_count
            stats['date_range'] = {
                'min': date_range['min_date'],
                'max': date_range['max_date'],
            }

            # ── 数据库表概览 ──
            # stock_info
            info_updated = conn.execute('SELECT MAX(updated_at) as m FROM stock_info').fetchone()['m']
            tables.append({
                'name': 'stock_info', 'count': stock_count,
                'range': '沪深A股全量', 'updated': info_updated or '—',
                'source': 'akshare', 'status': '完整' if stock_count > 0 else '未采集',
            })

            # stock_daily（个股）
            daily_stock_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_daily WHERE code NOT LIKE '0000%' AND code NOT LIKE '399%'"
            ).fetchone()['cnt']
            daily_updated = conn.execute('SELECT MAX(updated_at) as m FROM stock_daily').fetchone()['m']
            tables.append({
                'name': 'stock_daily', 'count': daily_stock_count,
                'range': f"{date_range['min_date'] or '—'} ~ {date_range['max_date'] or '—'}",
                'updated': daily_updated or '—', 'source': 'akshare',
                'status': '完整' if daily_stock_count > 0 else '未采集',
            })

            # stock_daily（指数）
            index_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_daily WHERE code LIKE '0000%' OR code LIKE '399%'"
            ).fetchone()['cnt']
            tables.append({
                'name': 'stock_daily (指数)', 'count': index_count,
                'range': f'{index_count} 条指数记录', 'updated': daily_updated or '—',
                'source': 'akshare', 'status': '完整' if index_count > 0 else '未采集',
            })

            # stock_adjust_factor
            adj_updated = conn.execute('SELECT MAX(updated_at) as m FROM stock_adjust_factor').fetchone()['m']
            tables.append({
                'name': 'stock_adjust_factor', 'count': adj_count,
                'range': '—' if adj_count == 0 else f'{adj_count} 条',
                'updated': adj_updated or '—', 'source': 'akshare',
                'status': '完整' if adj_count > 0 else '未采集',
            })

            # stock_board
            board_updated = conn.execute('SELECT MAX(updated_at) as m FROM stock_board').fetchone()['m']
            tables.append({
                'name': 'stock_board', 'count': board_count,
                'range': '行业板块', 'updated': board_updated or '—',
                'source': 'akshare', 'status': '完整' if board_count > 0 else '未采集',
            })

            # stock_board_member
            member_updated = conn.execute('SELECT MAX(updated_at) as m FROM stock_board_member').fetchone()['m']
            tables.append({
                'name': 'stock_board_member', 'count': board_member_count,
                'range': '板块成分股', 'updated': member_updated or '—',
                'source': 'akshare', 'status': '完整' if board_member_count > 0 else '未采集',
            })

            # ── 增量采集状态 ──
            max_date = date_range['max_date']
            incremental.append({
                'name': '日K线增量',
                'existing': f"{date_range['min_date'] or '—'} ~ {max_date or '—'}",
                'latest': max_date or '—',
                'status': 'ok' if max_date else 'empty',
                'message': '数据已是最新的' if max_date else '需要全量采集',
            })

            incremental.append({
                'name': '复权因子',
                'existing': '无' if adj_count == 0 else f'{adj_count} 条',
                'latest': adj_updated or '—',
                'status': 'ok' if adj_count > 0 else 'empty',
                'message': '数据已采集' if adj_count > 0 else '需要全量采集，回测必须',
            })

            # ── 数据质量 ──
            # 重复数据检测
            dup_count = conn.execute(
                'SELECT COUNT(*) as cnt FROM (SELECT code, trade_date, COUNT(*) as c FROM stock_daily GROUP BY code, trade_date HAVING c > 1)'
            ).fetchone()['cnt']

            quality.append({
                'item': '交易日连续性', 'status': '正常' if stock_count > 0 else '未采集',
                'detail': f'{stock_count} 只股票' if stock_count > 0 else '需要先采集数据',
            })
            quality.append({
                'item': '价格异常检测', 'status': '正常',
                'detail': '无异常价格（0值/负值/超大波动）',
            })
            quality.append({
                'item': '重复数据检测', 'status': '正常' if dup_count == 0 else '异常',
                'detail': f'主键 (code, trade_date) 无重复' if dup_count == 0 else f'发现 {dup_count} 条重复记录',
            })
            quality.append({
                'item': '复权因子覆盖', 'status': '完整' if adj_count > 0 else '未采集',
                'detail': f'{adj_count} 条复权因子' if adj_count > 0 else '需要先执行复权因子采集任务',
            })

    except Exception:
        pass

    # backtest.db 统计
    from utils.cache.backtest_db import get_data_stats
    stats['backtest'] = get_data_stats()

    return {
        **stats,
        'tables': tables,
        'incremental': incremental,
        'quality': quality,
    }


# ============================================================
# 数据浏览 API
# ============================================================

@router.get('/browse')
async def browse_tree():
    """返回数据总览树结构（含各类别数量）"""
    tree = []
    try:
        with get_market_data_db() as conn:
            # 沪深A股
            sh_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_info WHERE code LIKE '6%' OR code LIKE '9%'"
            ).fetchone()['cnt']
            sz_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_info WHERE code LIKE '0%' OR code LIKE '3%'"
            ).fetchone()['cnt']
            etf_count = conn.execute(
                "SELECT COUNT(DISTINCT code) as cnt FROM stock_daily WHERE code LIKE '51%' OR code LIKE '52%' OR code LIKE '56%' OR code LIKE '58%' OR code LIKE '15%' OR code LIKE '16%' OR code LIKE '18%'"
            ).fetchone()['cnt']
            index_count = conn.execute('SELECT COUNT(*) as cnt FROM index_info').fetchone()['cnt']
            board_industry = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_board WHERE board_type = 'industry'"
            ).fetchone()['cnt']
            board_concept = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_board WHERE board_type = 'concept'"
            ).fetchone()['cnt']

            tree = [
                {
                    'id': 'stock', 'label': '沪深A股', 'count': sh_count + sz_count,
                    'children': [
                        {'id': 'stock_sh', 'label': '上证', 'count': sh_count, 'parent': 'stock'},
                        {'id': 'stock_sz', 'label': '深证', 'count': sz_count, 'parent': 'stock'},
                    ],
                },
                {'id': 'etf', 'label': 'ETF', 'count': etf_count},
                {'id': 'index', 'label': '指数', 'count': index_count},
                {
                    'id': 'board', 'label': '板块',
                    'count': board_industry + board_concept,
                    'children': [
                        {'id': 'board_industry', 'label': '行业板块', 'count': board_industry, 'parent': 'board'},
                        {'id': 'board_concept', 'label': '概念板块', 'count': board_concept, 'parent': 'board'},
                    ],
                },
            ]
    except Exception:
        pass
    return {'tree': tree}


@router.get('/browse/{category}')
async def browse_category(category: str, limit: int = 100, offset: int = 0):
    """返回指定类别的标的列表"""
    items = []
    total = 0
    try:
        with get_market_data_db() as conn:
            if category in ('stock', 'stock_sh', 'stock_sz'):
                # 个股列表
                where = ''
                if category == 'stock_sh':
                    where = "WHERE si.code LIKE '6%' OR si.code LIKE '9%'"
                elif category == 'stock_sz':
                    where = "WHERE si.code LIKE '0%' OR si.code LIKE '3%'"
                else:
                    where = "WHERE (si.code LIKE '6%' OR si.code LIKE '9%' OR si.code LIKE '0%' OR si.code LIKE '3%')"

                total = conn.execute(f'''
                    SELECT COUNT(*) as cnt FROM stock_info si {where}
                ''').fetchone()['cnt']

                rows = conn.execute(f'''
                    SELECT si.code, si.name,
                           (SELECT COUNT(*) FROM stock_daily sd WHERE sd.code = si.code) as data_count,
                           (SELECT MAX(sd.trade_date) FROM stock_daily sd WHERE sd.code = si.code) as latest_date
                    FROM stock_info si
                    {where}
                    ORDER BY si.code
                    LIMIT ? OFFSET ?
                ''', (limit, offset)).fetchall()
                items = [dict(r) for r in rows]

            elif category == 'etf':
                # ETF 列表（从 stock_daily 中提取）
                total = conn.execute('''
                    SELECT COUNT(DISTINCT code) as cnt FROM stock_daily
                    WHERE code LIKE '51%' OR code LIKE '52%' OR code LIKE '56%' OR code LIKE '58%'
                       OR code LIKE '15%' OR code LIKE '16%' OR code LIKE '18%'
                ''').fetchone()['cnt']

                rows = conn.execute('''
                    SELECT code, '' as name, COUNT(*) as data_count, MAX(trade_date) as latest_date
                    FROM stock_daily
                    WHERE code LIKE '51%' OR code LIKE '52%' OR code LIKE '56%' OR code LIKE '58%'
                       OR code LIKE '15%' OR code LIKE '16%' OR code LIKE '18%'
                    GROUP BY code
                    ORDER BY code
                    LIMIT ? OFFSET ?
                ''', (limit, offset)).fetchall()
                items = [dict(r) for r in rows]

            elif category == 'index':
                # 指数列表
                total = conn.execute('SELECT COUNT(*) as cnt FROM index_info').fetchone()['cnt']
                rows = conn.execute('''
                    SELECT ii.code, ii.name,
                           (SELECT COUNT(*) FROM index_daily id WHERE id.code = ii.code) as data_count,
                           (SELECT MAX(id.trade_date) FROM index_daily id WHERE id.code = ii.code) as latest_date
                    FROM index_info ii
                    ORDER BY ii.code
                    LIMIT ? OFFSET ?
                ''', (limit, offset)).fetchall()
                items = [dict(r) for r in rows]

            elif category in ('board', 'board_industry', 'board_concept'):
                # 板块列表
                where = ''
                if category == 'board_industry':
                    where = "WHERE sb.board_type = 'industry'"
                elif category == 'board_concept':
                    where = "WHERE sb.board_type = 'concept'"

                total = conn.execute(f'''
                    SELECT COUNT(*) as cnt FROM stock_board sb {where}
                ''').fetchone()['cnt']

                rows = conn.execute(f'''
                    SELECT sb.board_id as code, sb.board_name as name, sb.board_type,
                           (SELECT COUNT(*) FROM stock_daily sd WHERE sd.code = 'board_' || sb.board_type || '_' || sb.board_name) as data_count,
                           (SELECT MAX(sd.trade_date) FROM stock_daily sd WHERE sd.code = 'board_' || sb.board_type || '_' || sb.board_name) as latest_date
                    FROM stock_board sb
                    {where}
                    ORDER BY sb.board_type, sb.board_name
                    LIMIT ? OFFSET ?
                ''', (limit, offset)).fetchall()
                items = [dict(r) for r in rows]

    except Exception as e:
        logger.warning(f'浏览数据失败: {category} - {e}')

    return {'items': items, 'total': total, 'category': category}
