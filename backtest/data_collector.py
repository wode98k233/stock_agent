"""数据采集器

负责批量采集股票数据到 market_data.db。
支持：
- 股票列表采集
- 日K线批量采集（增量）
- 指数日K线采集
- 复权因子采集
- 任务进度跟踪（data_tasks 表）
- pause / resume / cancel 控制
"""
import time
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable

import pandas as pd

from utils.cache.market_data_db import (
    get_market_data_db, upsert_stock_daily,
)
from utils.cache.backtest_db import (
    create_data_task, get_data_task, update_data_task, append_task_log,
)


class DataCollector:
    """数据采集器"""

    def __init__(self):
        self._cancel_flags: Dict[str, bool] = {}
        self._pause_flags: Dict[str, bool] = {}
        self._task_loggers: Dict[str, Any] = {}  # task_id -> TaskLogger

    def create_and_run(self, task_type: str, config: Dict[str, Any],
                       callback: Optional[Callable] = None) -> str:
        """创建任务并在后台线程中执行

        Args:
            task_type: stock_list / daily_kline / index_kline / adjust_factor / full_sync
            config: 任务配置
            callback: 完成回调 (task_id, result)

        Returns:
            task_id
        """
        import json
        import random
        task_id = f'T-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}'
        create_data_task(task_id, task_type, json.dumps(config, ensure_ascii=False))

        self._cancel_flags[task_id] = False
        self._pause_flags[task_id] = False

        def _run():
            try:
                self._execute_task(task_id, task_type, config)
                if callback:
                    callback(task_id, 'completed')
            except Exception as e:
                update_data_task(task_id, status='failed', error_message=str(e))
                append_task_log(task_id, f'[ERROR] 任务失败: {e}')
                if callback:
                    callback(task_id, 'failed')

        thread = threading.Thread(target=_run, daemon=True, name=f'data-collector-{task_id}')
        thread.start()
        return task_id

    def pause(self, task_id: str):
        self._pause_flags[task_id] = True
        update_data_task(task_id, status='paused')

    def resume(self, task_id: str):
        self._pause_flags[task_id] = False
        update_data_task(task_id, status='running')

    def cancel(self, task_id: str):
        self._cancel_flags[task_id] = True
        update_data_task(task_id, status='cancelled')

    def _check_flags(self, task_id: str):
        """检查 pause / cancel flag"""
        if self._cancel_flags.get(task_id):
            raise _CancelledException(f'任务 {task_id} 已取消')
        while self._pause_flags.get(task_id):
            time.sleep(1)
            if self._cancel_flags.get(task_id):
                raise _CancelledException(f'任务 {task_id} 已取消')

    def _tlog(self, task_id: str):
        """获取任务的详细日志器"""
        return self._task_loggers.get(task_id)

    @staticmethod
    def _get_sources(config: Dict[str, Any]):
        """获取数据源列表（按 source 参数过滤）"""
        from tools.fetcher import _ensure_initialized, DataSourceManager
        _ensure_initialized()
        sources = DataSourceManager.get_available_sources()
        source_name = config.get('source', 'auto')
        if source_name and source_name != 'auto':
            filtered = [s for s in sources if s.name == source_name]
            return filtered if filtered else sources
        return sources

    def _execute_task(self, task_id: str, task_type: str, config: Dict[str, Any]):
        """执行采集任务"""
        from utils.task_logger import TaskLogger
        tlog = TaskLogger(task_id)
        self._task_loggers[task_id] = tlog

        start_time = time.time()
        update_data_task(task_id, status='running', started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        append_task_log(task_id, f'[INFO] 任务开始，类型: {task_type}')
        tlog.task_start(task_type, config)

        try:
            if task_type == 'stock_list':
                self._collect_stock_list(task_id, config)
            elif task_type == 'daily_kline':
                self._collect_daily_kline(task_id, config)
            elif task_type == 'index_kline':
                self._collect_index_kline(task_id, config)
            elif task_type == 'adjust_factor':
                self._collect_adjust_factor(task_id, config)
            elif task_type == 'board_list':
                self._collect_board_list(task_id, config)
            elif task_type == 'board_member':
                self._collect_board_member(task_id, config)
            elif task_type == 'board_kline':
                self._collect_board_kline(task_id, config)
            elif task_type == 'full_sync':
                self._collect_stock_list(task_id, config)
                self._check_flags(task_id)
                self._collect_daily_kline(task_id, config)
                self._check_flags(task_id)
                self._collect_index_kline(task_id, config)
            elif task_type == 'hithink_sync':
                self._collect_hithink_sync(task_id, config)
            else:
                raise ValueError(f'未知任务类型: {task_type}')

            duration = round(time.time() - start_time, 2)
            update_data_task(task_id, status='completed',
                             completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                             duration_seconds=duration)
            append_task_log(task_id, f'[OK] 任务完成，耗时 {duration}s')
            tlog.task_end('completed', duration)

        except _CancelledException:
            duration = round(time.time() - start_time, 2)
            update_data_task(task_id, status='cancelled',
                             completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                             duration_seconds=duration)
            append_task_log(task_id, '[WARN] 任务已取消')
            tlog.task_end('cancelled', duration)
            raise
        except Exception as e:
            duration = round(time.time() - start_time, 2)
            update_data_task(task_id, status='failed',
                             completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                             duration_seconds=duration,
                             error_message=str(e))
            append_task_log(task_id, f'[ERROR] 任务失败: {e}')
            tlog.task_end('failed', duration)
            tlog.exception(f'任务异常: {e}')
            raise
        finally:
            self._task_loggers.pop(task_id, None)

    def _collect_stock_list(self, task_id: str, config: Dict[str, Any]):
        """采集全市场股票列表（走 DataSourceManager）"""
        source_name = config.get('source', 'auto')
        append_task_log(task_id, f'[INFO] 开始采集股票列表（数据源: {source_name}）...')

        tlog = self._tlog(task_id)
        sources = self._get_sources(config)
        df = None
        used_source = None
        for source in sources:
            method = getattr(source, 'get_spot_em', None) or getattr(source, 'get_stock_realtime', None)
            if not method:
                tlog and tlog.debug(f'[SOURCE] {source.name} 无 get_spot_em 方法，跳过')
                continue
            tlog and tlog.source_try(source.name, 'get_spot_em', '全市场')
            try:
                import time as _time
                _t0 = _time.time()
                df = method()
                elapsed = (_time.time() - _t0) * 1000
                if df is not None and not df.empty:
                    used_source = source.name
                    tlog and tlog.source_ok(source.name, len(df), elapsed)
                    break
                else:
                    tlog and tlog.source_fail(source.name, '返回空数据')
            except Exception as e:
                tlog and tlog.source_fail(source.name, str(e))
                continue

        if df is None or df.empty:
            append_task_log(task_id, '[WARN] 股票列表为空，所有数据源均不可用')
            return

        total = len(df)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 共 {total} 只股票（{used_source}）')
        tlog and tlog.info(f'数据源 {used_source} 返回 {total} 条')

        # 列名映射
        code_col = self._find_column(df, ['代码', 'code', '股票代码'])
        name_col = self._find_column(df, ['名称', 'name', '股票名称'])
        if not code_col or not name_col:
            append_task_log(task_id, f'[WARN] 数据源 {used_source} 返回的列名不匹配')
            return

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with get_market_data_db() as conn:
            for i, (_, row) in enumerate(df.iterrows()):
                self._check_flags(task_id)
                raw_code = str(row.get(code_col, ''))
                code = raw_code.zfill(6) if raw_code.isdigit() else raw_code
                name = str(row.get(name_col, ''))
                # 从代码推断市场
                market = 'SH' if code.startswith(('6', '9')) else 'SZ' if code.startswith(('0', '3')) else 'BJ' if code.startswith('4') else ''

                conn.execute('''INSERT OR REPLACE INTO stock_info
                    (code, name, market, source, updated_at)
                    VALUES (?, ?, ?, ?, ?)''',
                    (code, name, market, used_source, now))

                if (i + 1) % 100 == 0:
                    update_data_task(task_id, processed_count=i + 1,
                                     current_code=code, current_name=name)

            conn.commit()

        update_data_task(task_id, processed_count=total, success_count=total)
        append_task_log(task_id, f'[OK] 股票列表采集完成，共 {total} 只')

    def _collect_hithink_sync(self, task_id: str, config: Dict[str, Any]):
        """同花顺灌库：全市场快照 + 可选指定标的历史K线（source='hithink'）。"""
        append_task_log(task_id, '[INFO] 开始同花顺灌库（hithink_sync）...')
        tlog = self._tlog(task_id)

        try:
            from backtest.hithink_marketdb import sync_spot, sync_hist
        except Exception as e:
            append_task_log(task_id, f'[ERROR] 导入灌库模块失败: {e}')
            raise

        # 1) 全市场快照灌库（当日）
        append_task_log(task_id, '[INFO] 步骤1/2: 全市场快照灌库...')
        tlog and tlog.info('开始全市场快照灌库')
        try:
            n = sync_spot()
            append_task_log(task_id, f'[INFO] 快照灌库完成: {n} 条')
        except Exception as e:
            append_task_log(task_id, f'[WARN] 快照灌库失败（继续）: {e}')

        # 2) 指定标的的历史K线灌库
        codes = [c.strip() for c in (config.get('codes') or []) if c and str(c).strip()]
        if codes:
            append_task_log(task_id, f'[INFO] 步骤2/2: 历史K线灌库 {len(codes)} 只...')
            res = sync_hist(codes, start=config.get('start', ''), end=config.get('end', ''))
            append_task_log(task_id, f'[INFO] 历史K灌库: 成功 {res["ok"]} / 失败 {res["fail"]} / 写入 {res["total"]} 条')
        else:
            append_task_log(task_id, '[INFO] 未指定 codes，仅执行快照灌库')

    def _collect_daily_kline(self, task_id: str, config: Dict[str, Any]):
        """采集日K线数据"""
        codes = config.get('codes', [])
        days = config.get('days', 500)
        source = config.get('source', 'auto')

        if not codes:
            # 从 stock_info 获取所有代码
            with get_market_data_db() as conn:
                rows = conn.execute('SELECT code FROM stock_info').fetchall()
                codes = [r['code'] for r in rows]

        if not codes:
            append_task_log(task_id, '[WARN] 无股票代码，请先采集股票列表')
            return

        total = len(codes)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 开始采集日K线，共 {total} 只，{days} 天')

        success, failed = 0, 0
        for i, code in enumerate(codes):
            self._check_flags(task_id)
            update_data_task(task_id, processed_count=i, current_code=code)

            try:
                from utils.cache.market_data_db import fetch_and_store_daily
                count = fetch_and_store_daily(code, days=days, source=source)
                success += 1
                if (i + 1) % 50 == 0:
                    append_task_log(task_id, f'[OK] 进度 {i+1}/{total}，{code} 写入 {count} 条')
            except Exception as e:
                failed += 1
                append_task_log(task_id, f'[WARN] {code} 失败: {e}')

            update_data_task(task_id, success_count=success, failed_count=failed)

        update_data_task(task_id, processed_count=total)
        append_task_log(task_id, f'[OK] 日K线采集完成，成功 {success}，失败 {failed}')

    def _collect_index_kline(self, task_id: str, config: Dict[str, Any]):
        """采集指数日K线（写入 index_daily 表）"""
        default_indices = [
            # 综合指数
            ('000001', '上证指数'), ('399001', '深证成指'), ('399006', '创业板指'),
            ('000300', '沪深300'), ('000905', '中证500'), ('000016', '上证50'),
            ('000852', '中证1000'), ('000688', '科创50'), ('399005', '中小100'),
            ('000015', '红利指数'), ('000038', '上证180'), ('000134', '上证综指'),
            # 消费/白酒
            ('000932', '中证消费'), ('399997', '中证白酒'),
            # 医药/创新药
            ('000991', '全指医药'), ('399441', '生物医药'),
            # 新能源/光伏
            ('399808', '中证新能源'), ('399976', '新能源车'),
            # 以下需要网络好时验证代码格式
            # ('990001', '国证芯片'), ('930713', '人工智能'), ('931187', '科技龙头'),
            # ('930988', '中证黄金'),
            # ('HSTECH', '恒生科技'), ('H30533', '恒生互联'),
        ]
        indices = config.get('indices', default_indices)
        days = config.get('days', 500)

        total = len(indices)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 开始采集指数日K线，共 {total} 只，写入 index_daily 表')

        for i, (code, name) in enumerate(indices):
            self._check_flags(task_id)
            update_data_task(task_id, processed_count=i, current_code=code, current_name=name)

            try:
                from utils.cache.market_data_db import fetch_and_store_index_daily
                count = fetch_and_store_index_daily(code, name=name, days=days)
                append_task_log(task_id, f'[OK] {name}({code}) 写入 {count} 条')
            except Exception as e:
                append_task_log(task_id, f'[WARN] {name}({code}) 失败: {e}')

        update_data_task(task_id, processed_count=total)
        append_task_log(task_id, '[OK] 指数日K线采集完成')

    def _collect_adjust_factor(self, task_id: str, config: Dict[str, Any]):
        """采集复权因子"""
        codes = config.get('codes', [])

        if not codes:
            with get_market_data_db() as conn:
                rows = conn.execute('SELECT code FROM stock_info').fetchall()
                codes = [r['code'] for r in rows]

        if not codes:
            append_task_log(task_id, '[WARN] 无股票代码，请先采集股票列表')
            return

        total = len(codes)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 开始采集复权因子，共 {total} 只')

        success, failed = 0, 0
        for i, code in enumerate(codes):
            self._check_flags(task_id)
            update_data_task(task_id, processed_count=i, current_code=code)

            try:
                self._fetch_adjust_factor(code, days=config.get('days', 500))
                success += 1
                if (i + 1) % 100 == 0:
                    append_task_log(task_id, f'[OK] 进度 {i+1}/{total}')
            except Exception as e:
                failed += 1
                if (i + 1) % 100 == 0:
                    append_task_log(task_id, f'[WARN] {code} 失败: {e}')

            update_data_task(task_id, success_count=success, failed_count=failed)

        update_data_task(task_id, processed_count=total)
        append_task_log(task_id, f'[OK] 复权因子采集完成，成功 {success}，失败 {failed}')

    def _fetch_adjust_factor(self, code: str, days: int = 500):
        """通过不复权/前复权价格反推前复权+后复权因子

        拿 2 次 API（raw + qfq），后复权因子 = 1 / 前复权因子。
        支持个股和 ETF（自动识别）。
        """
        try:
            from datetime import timedelta
            from tools.fetcher.akshare_ds import AkshareDataSource
            AkshareDataSource._ensure_patch()
            import akshare as ak
            from utils.cache.market_data_db import _detect_market

            start_date = (datetime.now() - timedelta(days=days + 60)).strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')

            market = _detect_market(code)
            is_etf = market == 'ETF'

            # 获取不复权数据
            if is_etf:
                df_raw = ak.fund_etf_hist_em(symbol=code, period='daily', adjust='', start_date=start_date, end_date=end_date)
            else:
                df_raw = ak.stock_zh_a_hist(symbol=code, period='daily', adjust='', start_date=start_date, end_date=end_date)
            if df_raw is None or df_raw.empty:
                return

            # 获取前复权数据
            if is_etf:
                df_qfq = ak.fund_etf_hist_em(symbol=code, period='daily', adjust='qfq', start_date=start_date, end_date=end_date)
            else:
                df_qfq = ak.stock_zh_a_hist(symbol=code, period='daily', adjust='qfq', start_date=start_date, end_date=end_date)
            if df_qfq is None or df_qfq.empty:
                return

            # 按日期索引不复权收盘价（ETF 和个股列名不同）
            date_col = '日期'
            close_col = '收盘'
            raw_map = {}
            for _, row in df_raw.iterrows():
                date_str = str(row.get(date_col, ''))
                raw_map[date_str] = float(row.get(close_col, 0))

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            count = 0
            with get_market_data_db() as conn:
                for _, row in df_qfq.iterrows():
                    date_str = str(row.get(date_col, ''))
                    qfq_close = float(row.get(close_col, 0))
                    raw_close = raw_map.get(date_str, 0)
                    if raw_close > 0 and qfq_close > 0:
                        fore_factor = round(qfq_close / raw_close, 6)
                        back_factor = round(raw_close / qfq_close, 6)
                        conn.execute('''INSERT OR REPLACE INTO stock_adjust_factor
                            (code, trade_date, fore_adjust_factor, back_adjust_factor, source, updated_at)
                            VALUES (?, ?, ?, ?, 'akshare', ?)''',
                            (code, date_str, fore_factor, back_factor, now))
                        count += 1
                conn.commit()

            if count > 0:
                append_task_log('', f'[OK] {code} 写入 {count} 条复权因子')

        except Exception as e:
            raise RuntimeError(f'{code} 复权因子获取失败: {e}') from e

    def _collect_board_list(self, task_id: str, config: Dict[str, Any]):
        """采集板块列表（行业 + 概念）

        通过 DataSourceManager 遍历可用源，用第一个成功的，不合并。
        """
        tlog = self._tlog(task_id)
        board_type = config.get('board_type', 'all')

        types_to_collect = []
        if board_type in ('industry', 'all'):
            types_to_collect.append('industry')
        if board_type in ('concept', 'all'):
            types_to_collect.append('concept')

        sources = self._get_sources(config)
        tlog and tlog.info(f'可用数据源: {[s.name for s in sources]}')

        total = 0
        for btype in types_to_collect:
            self._check_flags(task_id)
            method_name = f'get_board_{btype}_list'
            append_task_log(task_id, f'[INFO] 开始采集{btype}板块列表...')
            tlog and tlog.info(f'开始采集 {btype} 板块列表')

            success = False
            for source in sources:
                method = getattr(source, method_name, None)
                if not method:
                    tlog and tlog.debug(f'[SOURCE] {source.name} 无 {method_name}，跳过')
                    continue
                try:
                    tlog and tlog.source_try(source.name, method_name, btype)
                    import time as _time
                    _t0 = _time.time()
                    df = method()
                    elapsed = (_time.time() - _t0) * 1000
                    if df is None or df.empty:
                        tlog and tlog.source_fail(source.name, '返回空数据')
                        continue

                    # 统一列名：以 akshare 字段为标准
                    code_col = self._find_column(df, ['板块代码', 'code', '代码'])
                    name_col = self._find_column(df, ['板块名称', 'name', '名称'])
                    if not code_col or not name_col:
                        tlog and tlog.source_fail(source.name, f'列名不匹配: {list(df.columns)}')
                        continue
                    tlog and tlog.source_ok(source.name, len(df), elapsed)

                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    with get_market_data_db() as conn:
                        for _, row in df.iterrows():
                            board_code = str(row.get(code_col, ''))
                            board_name = str(row.get(name_col, ''))
                            board_id = f'{btype}_{board_code}'
                            conn.execute('''INSERT OR REPLACE INTO stock_board
                                (board_id, board_code, board_name, board_type, provider, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?)''',
                                (board_id, board_code, board_name, btype, source.name, now))
                            total += 1
                        conn.commit()

                    append_task_log(task_id, f'[OK] {btype}板块: {len(df)} 个（{source.name}）')
                    success = True
                    break
                except Exception:
                    continue

            if not success:
                append_task_log(task_id, f'[WARN] {btype}板块采集失败，所有数据源均不可用')

        update_data_task(task_id, success_count=total)
        append_task_log(task_id, f'[OK] 板块列表采集完成，共 {total} 个')

    @staticmethod
    def _find_column(df, candidates):
        """从 DataFrame 中找到第一个存在的列名"""
        for col in candidates:
            if col in df.columns:
                return col
        return None

    def _collect_board_member(self, task_id: str, config: Dict[str, Any]):
        """采集板块成分股

        通过 DataSourceManager 遍历可用源，用第一个成功的，不合并。
        """
        tlog = self._tlog(task_id)
        board_names = config.get('boards', [])
        if not board_names:
            with get_market_data_db() as conn:
                rows = conn.execute('SELECT board_name, board_type FROM stock_board').fetchall()
                board_names = [(r['board_name'], r['board_type']) for r in rows]

        if not board_names:
            msg = '[WARN] 无板块数据，请先采集板块列表'
            append_task_log(task_id, msg)
            tlog and tlog.warning(msg)
            return

        total = len(board_names)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 开始采集板块成分，共 {total} 个板块')
        tlog and tlog.info(f'开始采集板块成分，共 {total} 个板块，数据源: {config.get("source", "auto")}')

        success, failed = 0, 0
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for i, item in enumerate(board_names):
            self._check_flags(task_id)
            if isinstance(item, (list, tuple)):
                board_name, board_type = item
            else:
                board_name = item
                board_type = 'industry'

            update_data_task(task_id, processed_count=i, current_code=board_name)
            tlog and tlog.info(f'[{i+1}/{total}] 开始处理: {board_name} ({board_type})')

            # 从 stock_board 表查 board_id，提取 BK 代码（efinance 需要）
            bk_code = None
            board_id = f'{board_type}_{board_name}'
            try:
                with get_market_data_db() as conn:
                    row = conn.execute(
                        'SELECT board_id FROM stock_board WHERE board_name = ? AND board_type = ?',
                        (board_name, board_type)
                    ).fetchone()
                    if row:
                        board_id = row['board_id']
                        # board_id 格式: industry_BK1215 → 提取 BK1215
                        parts = board_id.split('_', 1)
                        if len(parts) == 2 and parts[1].startswith('BK'):
                            bk_code = parts[1]
            except Exception:
                pass

            got_it = False
            source_filter = config.get('source', 'auto')

            # 优先：东财 push2 直连（按 BK 代码），单请求最轻量、最不易被掐
            if bk_code and source_filter in ('auto', 'akshare', 'eastmoney'):
                try:
                    from tools.fetcher.eastmoney_board import fetch_board_cons_by_bk
                    df = fetch_board_cons_by_bk(bk_code)
                    if df is not None and not df.empty:
                        with get_market_data_db() as conn:
                            for _, row in df.iterrows():
                                conn.execute('''INSERT OR REPLACE INTO stock_board_member
                                    (board_id, code, as_of_date, source, updated_at)
                                    VALUES (?, ?, ?, ?, ?)''',
                                    (board_id, str(row.get('代码', '')), now[:10], 'eastmoney_bk', now))
                            conn.commit()
                        success += 1
                        got_it = True
                        append_task_log(task_id, f'[OK] {board_name}: {len(df)} 只（eastmoney_bk）')
                        tlog and tlog.source_ok('eastmoney_bk', len(df), 0)
                except Exception as e:
                    tlog and tlog.source_fail('eastmoney_bk', str(e))

            # 次选：遍历可用源，用第一个成功的
            sources = [] if got_it else self._get_sources(config)
            for source in sources:
                method_name = f'get_board_{board_type}_cons'
                method = getattr(source, method_name, None)
                if not method:
                    tlog and tlog.debug(f'  [{source.name}] 无 {method_name} 方法，跳过')
                    continue
                try:
                    tlog and tlog.source_try(source.name, method_name, board_name)
                    import time as _time
                    _t0 = _time.time()
                    df = method(symbol=board_name)
                    elapsed = (_time.time() - _t0) * 1000

                    if df is None or df.empty:
                        tlog and tlog.source_fail(source.name, '返回空数据')
                        continue

                    # 统一列名
                    code_col = self._find_column(df, ['代码', 'code', '股票代码'])
                    if not code_col:
                        tlog and tlog.source_fail(source.name, f'列名不匹配: {list(df.columns)}')
                        continue

                    tlog and tlog.source_ok(source.name, len(df), elapsed)

                    with get_market_data_db() as conn:
                        for _, row in df.iterrows():
                            code = str(row.get(code_col, ''))
                            conn.execute('''INSERT OR REPLACE INTO stock_board_member
                                (board_id, code, as_of_date, source, updated_at)
                                VALUES (?, ?, ?, ?, ?)''',
                                (board_id, code, now[:10], source.name, now))
                        conn.commit()

                    tlog and tlog.db_write('stock_board_member', len(df))
                    success += 1
                    got_it = True
                    append_task_log(task_id, f'[OK] {board_name}: {len(df)} 只（{source.name}）')
                    tlog and tlog.progress(i + 1, total, f'{board_name}: {len(df)} 只')
                    break
                except Exception as e:
                    tlog and tlog.source_fail(source.name, str(e))
                    continue

            if not got_it:
                failed += 1
                msg = f'[WARN] {board_name} 失败，所有数据源均不可用'
                append_task_log(task_id, msg)
                tlog and tlog.warning(msg)

            update_data_task(task_id, success_count=success, failed_count=failed)

        update_data_task(task_id, processed_count=total)
        summary = f'成功 {success}，失败 {failed}'
        append_task_log(task_id, f'[OK] 板块成分采集完成，{summary}')
        tlog and tlog.info(f'板块成分采集完成，{summary}')

    def _collect_board_kline(self, task_id: str, config: Dict[str, Any]):
        """采集板块K线数据

        优先走 DataSourceManager（东财），fallback 到 THS。
        数据写入 stock_daily 表（用 board_ 前缀区分代码）。
        """
        tlog = self._tlog(task_id)
        from tools.fetcher.patches.eastmoney_patch import eastmoney_patch
        eastmoney_patch()
        import akshare as ak

        days = config.get('days', 500)

        boards = config.get('boards', [])
        if not boards:
            board_type = config.get('board_type', 'all')
            with get_market_data_db() as conn:
                if board_type == 'all':
                    rows = conn.execute('SELECT board_name, board_type FROM stock_board').fetchall()
                else:
                    rows = conn.execute('SELECT board_name, board_type FROM stock_board WHERE board_type = ?', (board_type,)).fetchall()
            boards = [(r['board_name'], r['board_type']) for r in rows]

        if not boards:
            msg = '[WARN] 无板块数据，请先采集板块列表'
            append_task_log(task_id, msg)
            tlog and tlog.warning(msg)
            return

        total = len(boards)
        update_data_task(task_id, total_count=total, processed_count=0)
        append_task_log(task_id, f'[INFO] 开始采集板块K线，共 {total} 个板块')
        tlog and tlog.info(f'开始采集板块K线，共 {total} 个板块')

        success, failed = 0, 0
        for i, (board_name, btype) in enumerate(boards):
            self._check_flags(task_id)
            update_data_task(task_id, processed_count=i, current_code=board_name)
            got_it = False

            # 优先走 DataSourceManager
            sources = self._get_sources(config)
            for source in sources:
                method = getattr(source, 'get_board_industry_hist', None)
                if not method:
                    continue
                try:
                    df = method(symbol=board_name, period='daily')
                    if df is None or df.empty:
                        continue
                    df = self._normalize_board_kline(df)
                    if df.empty:
                        continue
                    df = df.tail(days)
                    board_code = f'board_{btype}_{board_name}'
                    records = df.to_dict('records')
                    for r in records:
                        r['code'] = board_code
                    count = upsert_stock_daily(records, source=source.name)
                    success += 1
                    got_it = True
                    if (i + 1) % 10 == 0:
                        append_task_log(task_id, f'[OK] 进度 {i+1}/{total}，{board_name}: {count} 条（{source.name}）')
                    break
                except Exception:
                    continue

            # fallback: THS（未注册到 DataSourceManager）
            if not got_it:
                try:
                    if btype == 'concept':
                        df = ak.stock_board_concept_index_ths(symbol=board_name, start_date='20240101', end_date='20261231')
                    else:
                        df = ak.stock_board_industry_index_ths(symbol=board_name, start_date='20240101', end_date='20261231')
                    if df is not None and not df.empty:
                        df = self._normalize_board_kline(df, col_style='ths')
                        df = df.tail(days)
                        board_code = f'board_{btype}_{board_name}'
                        records = df.to_dict('records')
                        for r in records:
                            r['code'] = board_code
                        count = upsert_stock_daily(records, source='ths')
                        success += 1
                        got_it = True
                        if (i + 1) % 10 == 0:
                            append_task_log(task_id, f'[OK] 进度 {i+1}/{total}，{board_name}: {count} 条（ths）')
                except Exception:
                    pass

            if not got_it:
                failed += 1
                if (i + 1) % 10 == 0:
                    append_task_log(task_id, f'[WARN] {board_name} 失败，所有数据源均不可用')

            update_data_task(task_id, success_count=success, failed_count=failed)

        update_data_task(task_id, processed_count=total)
        append_task_log(task_id, f'[OK] 板块K线采集完成，成功 {success}，失败 {failed}')

    @staticmethod
    def _normalize_board_kline(df, col_style='em'):
        """统一板块K线列名"""
        if col_style == 'ths':
            col_map = {
                '日期': 'trade_date',
                '开盘价': 'open', '最高价': 'high', '最低价': 'low', '收盘价': 'close',
                '成交量': 'volume', '成交额': 'amount',
            }
        else:
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
        if 'close' in df.columns:
            df['pct_change'] = df['close'].pct_change() * 100
            df['change_amount'] = df['close'].diff()
        return df


class _CancelledException(Exception):
    pass
