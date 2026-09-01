"""批量回测引擎

并发调度 BacktestEngine，对一组 codes 执行同一策略 + 同一配置，
失败隔离、进度回调、结果聚合，最终落库到 backtest_batch / backtest_batch_item。

设计要点：
- 每个线程独立 BacktestEngine 实例（Cerebro 非线程安全）
- 单 code 失败不阻断整批
- 内存中只保存统计指标；equity_curve / trades 由前端按需通过 run_id 拉取
- 聚合结果作为快照写入 backtest_batch 表
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from backtest.engine import BacktestEngine
from utils.cache.backtest_db import (
    get_batch_items,
    update_batch,
    update_batch_item,
)

logger = logging.getLogger(__name__)

# 并发上限硬截断（防 OOM + 防 akshare 频率限制）
MAX_CONCURRENCY_HARD_LIMIT = 5

# 单 code 失败的 error_message 落库长度上限
ERROR_MESSAGE_MAX_LEN = 500


class BatchBacktestEngine:
    """批量回测引擎

    并发执行同一策略 + 同一配置在多个 code 上的回测。
    """

    def __init__(self, max_concurrency: int = MAX_CONCURRENCY_HARD_LIMIT):
        self.max_concurrency = max(1, min(max_concurrency, MAX_CONCURRENCY_HARD_LIMIT))

    def run(
        self,
        codes: List[str],
        config: Dict[str, Any],
        batch_id: str,
        run_ids: List[str],
        progress_callback: Optional[Callable[[str, str, str, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """并发执行批量回测

        Args:
            codes: 标的代码列表（已去重、校验、大写）
            config: 共享回测配置（不含 code；每个 code 会拷贝并填入 code/asset_type 字段）
            batch_id: 批次 ID
            run_ids: 与 codes 等长的 run_id 列表
            progress_callback: (batch_id, code, status, current, total) 回调

        Returns:
            {
                'batch_id': str,
                'status': 'completed' | 'partial' | 'failed',
                'success_count': int,
                'failed_count': int,
                'aggregates': {...},
                'items': [{code, run_id, status, ...}, ...],
            }
        """
        if len(codes) != len(run_ids):
            raise ValueError(f'codes ({len(codes)}) 与 run_ids ({len(run_ids)}) 长度不一致')

        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_batch(batch_id, status='running', started_at=started_at)

        total = len(codes)
        concurrency = self._resolve_concurrency(config.get('concurrency', 3))
        items_result: List[Dict[str, Any]] = []
        completed_count = 0

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_map = {
                executor.submit(self._run_single, code, config, run_id): (code, run_id)
                for code, run_id in zip(codes, run_ids)
            }
            for future in as_completed(future_map):
                code, run_id = future_map[future]
                try:
                    result = future.result()
                    self._on_code_success(batch_id, code, result)
                    items_result.append({
                        'code': code,
                        'run_id': run_id,
                        'status': 'completed',
                        **_pick_metrics(result),
                    })
                    if progress_callback:
                        progress_callback(batch_id, code, 'completed', completed_count + 1, total)
                except Exception as e:
                    logger.warning(f'批次 {batch_id} code={code} 失败: {e}')
                    self._on_code_failure(batch_id, code, e)
                    items_result.append({
                        'code': code,
                        'run_id': run_id,
                        'status': 'failed',
                        'error': str(e),
                    })
                    if progress_callback:
                        progress_callback(batch_id, code, 'failed', completed_count + 1, total)
                completed_count += 1

        # 聚合
        aggregates = self._aggregate(items_result)
        success_count = sum(1 for it in items_result if it['status'] == 'completed')
        failed_count = total - success_count
        final_status = (
            'completed' if failed_count == 0
            else ('partial' if success_count > 0 else 'failed')
        )

        completed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 起止时间从 update_batch 的 started_at 推算
        duration_seconds = _calc_duration_seconds(started_at, completed_at)
        update_batch(
            batch_id,
            status=final_status,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            **aggregates,
        )

        return {
            'batch_id': batch_id,
            'status': final_status,
            'success_count': success_count,
            'failed_count': failed_count,
            'aggregates': aggregates,
            'items': items_result,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_concurrency(self, requested: int) -> int:
        """并发数受 max_concurrency 上限约束"""
        try:
            n = int(requested)
        except (TypeError, ValueError):
            n = 3
        return max(1, min(n, self.max_concurrency))

    def _run_single(self, code: str, base_config: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        """单 code 回测（在线程池中执行）

        每个线程独立 BacktestEngine 实例，Cerebro 不跨线程共享。
        """
        config = {**base_config, 'code': code}
        config['asset_type'] = _auto_detect_asset_type(code)
        # 单 code 回测内部的 bar 级进度不透传到批量层
        engine = BacktestEngine()
        return engine.run(config, run_id=run_id, progress_callback=None)

    def _on_code_success(self, batch_id: str, code: str, result: Dict[str, Any]) -> None:
        """单 code 成功，落 item 表"""
        update_batch_item(
            batch_id, code,
            status='completed',
            run_id=result.get('run_id'),
            total_return=result.get('total_return'),
            annual_return=result.get('annual_return'),
            sharpe_ratio=result.get('sharpe_ratio'),
            max_drawdown=result.get('max_drawdown'),
            win_rate=result.get('win_rate'),
            trade_count=result.get('trade_count'),
            final_equity=result.get('final_equity'),
            completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _on_code_failure(self, batch_id: str, code: str, exc: Exception) -> None:
        """单 code 失败，落 item 表"""
        msg = str(exc) or exc.__class__.__name__
        update_batch_item(
            batch_id, code,
            status='failed',
            error_message=msg[:ERROR_MESSAGE_MAX_LEN],
            completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _aggregate(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """聚合统计（仅对 status=completed 的 item）"""
        completed = [it for it in items if it.get('status') == 'completed']
        if not completed:
            return {
                'avg_total_return': 0.0,
                'avg_annual_return': 0.0,
                'avg_sharpe': 0.0,
                'avg_max_drawdown': 0.0,
                'avg_win_rate': 0.0,
                'profit_count': 0,
                'loss_count': 0,
            }
        total_returns = [it.get('total_return') or 0 for it in completed]
        annual_returns = [it.get('annual_return') or 0 for it in completed]
        sharpes = [it.get('sharpe_ratio') or 0 for it in completed if it.get('sharpe_ratio')]
        drawdowns = [it.get('max_drawdown') or 0 for it in completed]
        win_rates = [it.get('win_rate') or 0 for it in completed]
        return {
            'avg_total_return': _mean(total_returns),
            'avg_annual_return': _mean(annual_returns),
            'avg_sharpe': _mean(sharpes) if sharpes else 0.0,
            'avg_max_drawdown': _mean(drawdowns),
            'avg_win_rate': _mean(win_rates),
            'profit_count': sum(1 for r in total_returns if r > 0),
            'loss_count': sum(1 for r in total_returns if r < 0),
        }


# ----------------------------------------------------------------------
# 辅助函数（模块级，便于单测）
# ----------------------------------------------------------------------

def _auto_detect_asset_type(code: str) -> str:
    """根据代码格式自动识别资产类型

    规则:
        - board_* → 板块
        - 51/52/56/58/15/16/18 开头的 6 位数字 → ETF
        - 399xxx → 深证指数（仅 399 系列判定为 index，
          000xxx 既可能是上证指数也可能是深市个股，统一按 stock 处理，
          数据加载层会根据表查询自动区分）
        - 其他 6 位数字 → 个股
    """
    if not code:
        return 'stock'
    if code.startswith('board_'):
        return 'board'
    # 6 位数字代码规则
    if re.match(r'^(51|52|56|58|15|16|18)\d{4}$', code):
        return 'etf'
    if re.match(r'^399\d{3}$', code):
        return 'index'
    return 'stock'


def _mean(values: List[float]) -> float:
    """安全求平均（空列表返回 0）"""
    return sum(values) / len(values) if values else 0.0


def _pick_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 BacktestEngine 返回的 result 中提取批量层关注的指标"""
    return {
        'total_return': result.get('total_return'),
        'annual_return': result.get('annual_return'),
        'sharpe_ratio': result.get('sharpe_ratio'),
        'max_drawdown': result.get('max_drawdown'),
        'win_rate': result.get('win_rate'),
        'trade_count': result.get('trade_count'),
        'final_equity': result.get('final_equity'),
    }


def _calc_duration_seconds(started_at: str, completed_at: str) -> float:
    """从两个 'YYYY-MM-DD HH:MM:SS' 字符串计算耗时（秒）"""
    try:
        fmt = '%Y-%m-%d %H:%M:%S'
        start = datetime.strptime(started_at, fmt)
        end = datetime.strptime(completed_at, fmt)
        return round((end - start).total_seconds(), 2)
    except (ValueError, TypeError):
        return 0.0


def recompute_batch_summary(batch_id: str) -> Dict[str, Any]:
    """根据当前 batch_items 重新计算 batch 汇总字段和 status。

    单个重试完成后调用，确保 batch 主表反映最新状态。
    （批量重试走 BatchBacktestEngine.run，末尾自带 _aggregate + update_batch，无需调此函数。）
    """
    items = get_batch_items(batch_id)
    # 复用 _aggregate 的聚合逻辑（仅为复用，无实际并发）
    engine = BatchBacktestEngine()
    aggregates = engine._aggregate(items)

    success = sum(1 for it in items if it.get('status') == 'completed')
    failed = sum(1 for it in items if it.get('status') == 'failed')
    pending = sum(1 for it in items if it.get('status') in ('pending', 'running'))

    if pending > 0:
        final_status = 'running'
    elif failed == 0:
        final_status = 'completed'
    elif success > 0:
        final_status = 'partial'
    else:
        final_status = 'failed'

    update_batch(batch_id, status=final_status, **aggregates)
    return {'status': final_status, 'success_count': success, 'failed_count': failed}
