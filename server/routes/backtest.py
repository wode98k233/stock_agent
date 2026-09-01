"""回测 API

提供策略查询、回测执行、结果查询等接口。
回测通过后台线程异步执行，结果持久化到 backtest.db。
支持 SSE 进度推送。
"""
import json
import uuid
import asyncio
import threading
import logging
import pandas as pd
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from backtest.json_compiler import get_strategy_meta
from utils.cache.backtest_db import (
    get_strategies, get_strategy,
    get_backtest_run, get_backtest_runs,
    get_backtest_result, get_backtest_trades,
    update_backtest_run, save_backtest_result, save_backtest_trades,
    delete_backtest_run,
    upsert_strategy, delete_strategy,
    # 批量回测相关
    create_batch, get_batch, list_batches, update_batch, delete_batch,
    create_batch_items, get_batch_items, update_batch_item, get_batch_item_by_code,
    create_test_set, get_test_set, get_test_set_by_name, list_test_sets,
    update_test_set, delete_test_set,
    _resolve_stock_name,
    # 多策略批量回测
    create_multi, get_multi, list_multi, update_multi, delete_multi,
    create_multi_items, get_multi_items, update_multi_item,
)
from backtest.batch_engine import _auto_detect_asset_type

router = APIRouter(prefix='/api/backtest', tags=['backtest'])

# 回测进度存储（内存）: run_id -> {current, total, pct, status}
_backtest_progress: Dict[str, Dict[str, Any]] = {}

# 批量回测进度存储（内存）: batch_id -> {current, total, pct, status, last_code, last_status}
_batch_progress: Dict[str, Dict[str, Any]] = {}


def update_backtest_progress(run_id: str, current: int, total: int, pct: float):
    """由引擎回调，更新回测进度"""
    _backtest_progress[run_id] = {
        'current': current,
        'total': total,
        'pct': round(pct, 4),
        'status': 'running',
    }


def _finish_backtest_progress(run_id: str, status: str = 'completed'):
    """标记进度完成"""
    if run_id in _backtest_progress:
        _backtest_progress[run_id]['status'] = status
        _backtest_progress[run_id]['pct'] = 1.0


# ============================================================
# 请求模型
# ============================================================

class RunBacktestRequest(BaseModel):
    code: str
    strategy_id: str
    params: Dict[str, Any] = {}
    start_date: str = '2025-01-01'
    end_date: str = '2026-06-01'
    initial_cash: float = 100000
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.001
    t_plus_1: bool = True
    lot_size: int = 100
    adjust_type: str = 'qfq'
    stop_loss: float = 0
    take_profit: float = 0
    trailing_stop: float = 0
    position_mode: str = 'full'
    position_size: float = 1.0
    asset_type: str = 'stock'  # stock / etf / board


# ============================================================
# 后台回测执行
# ============================================================

def _run_backtest_async(run_id: str, config: dict):
    """后台线程执行回测"""
    # 立即标记为 running
    update_backtest_run(run_id, status='running', started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    _backtest_progress[run_id] = {'current': 0, 'total': 0, 'pct': 0, 'status': 'running'}
    try:
        from backtest.engine import BacktestEngine
        engine = BacktestEngine()

        # 进度回调
        def on_progress(current, total, pct):
            update_backtest_progress(run_id, current, total, pct)

        result = engine.run(config, run_id=run_id, progress_callback=on_progress)
        _finish_backtest_progress(run_id, 'completed')
        # engine.run 内部会更新 status=completed
    except Exception as e:
        _finish_backtest_progress(run_id, 'failed')
        update_backtest_run(
            run_id, status='failed',
            error_message=str(e),
            completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )


# ============================================================
# API 端点
# ============================================================

@router.get('/strategy-meta')
async def get_strategy_meta_endpoint():
    """返回策略构建器的元数据：可用指标、运算符、数据引用、模板"""
    return get_strategy_meta()


@router.get('/strategies')
async def list_strategies(category: Optional[str] = None):
    """获取可用策略列表"""
    strategies = get_strategies(category=category)
    for s in strategies:
        for field in ['params_schema', 'tags']:
            if s.get(field):
                try:
                    s[field] = json.loads(s[field])
                except (json.JSONDecodeError, TypeError):
                    pass
    return {'strategies': strategies}


@router.get('/strategies/{strategy_id}')
async def get_strategy_detail(strategy_id: str):
    """获取策略详情"""
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f'策略不存在: {strategy_id}')
    for field in ['params_schema', 'tags']:
        if strategy.get(field):
            try:
                strategy[field] = json.loads(strategy[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return strategy


# ============================================================
# 用户策略 CRUD
# ============================================================

class SaveStrategyRequest(BaseModel):
    strategy_id: str
    name: str
    description: str = ''
    strategy_json: Dict[str, Any]  # 完整的策略 JSON 定义
    category: str = 'custom'


@router.post('/strategies')
async def save_strategy(req: SaveStrategyRequest):
    """创建/更新用户策略

    接收 strategy-schema 格式的完整 JSON，校验后写入 strategies 表。
    内置策略的 strategy_id 不可被用户覆盖。
    """
    # 检查是否内置策略
    existing = get_strategy(req.strategy_id)
    if existing and existing.get('is_builtin'):
        raise HTTPException(status_code=400, detail=f'内置策略不可覆盖: {req.strategy_id}')

    # 校验 JSON 可编译
    try:
        from backtest.json_compiler import compile_strategy
        strategy_json_str = json.dumps(req.strategy_json, ensure_ascii=False)
        compile_strategy(strategy_json_str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'策略 JSON 无效: {e}')

    # 从 JSON 提取 params_schema
    raw_params = req.strategy_json.get('params', {})
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

    meta = req.strategy_json.get('meta', {})
    tags = json.dumps(meta.get('tags', []), ensure_ascii=False)

    upsert_strategy(
        strategy_id=req.strategy_id,
        name=req.name,
        description=req.description,
        category=req.category,
        params_schema=json.dumps(params_schema, ensure_ascii=False),
        is_builtin=False,
        code=json.dumps(req.strategy_json, ensure_ascii=False),
        tags=tags,
    )

    # 重新加载注册表
    try:
        from backtest.strategies import load_json_strategies
        load_json_strategies(force=True)
    except Exception:
        pass

    return {'status': 'ok', 'strategy_id': req.strategy_id}


@router.delete('/strategies/{strategy_id}')
async def delete_strategy_endpoint(strategy_id: str):
    """删除用户策略"""
    try:
        deleted = delete_strategy(strategy_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f'策略不存在: {strategy_id}')
    return {'status': 'ok', 'deleted': strategy_id}


# ============================================================
# Agent 自然语言生成策略
# ============================================================

_log = logging.getLogger("server.routes.backtest")

# 策略生成的系统提示词（精简版，告诉 LLM strategy-schema 格式）
_STRATEGY_GEN_SYSTEM_PROMPT = """你是一个量化策略生成器。根据用户的自然语言描述，输出一个回测策略的 JSON 定义。

输出格式必须严格遵守以下 schema：

{
  "meta": {
    "name": "策略名称",
    "description": "一句话描述核心逻辑",
    "tags": ["标签1", "标签2"],
    "source": "agent"
  },
  "params": {
    "参数名": { "value": 默认值, "min": 最小值, "max": 最大值, "step": 步长, "label": "中文显示名", "type": "int或float" }
  },
  "conditions": {
    "buy": {
      "logic": "AND",
      "rules": [
        {
          "type": "indicator",
          "left": { "func": "指标名", "args": ["数据源", 参数值] },
          "op": "运算符",
          "right": { "value": 值 或 { "func": "指标名", "args": [...] } }
        }
      ]
    },
    "sell": { "logic": "AND", "rules": [...] }
  },
  "risk": {
    "stop_loss": { "type": "fixed", "value": 0.05 },
    "take_profit": { "type": "fixed", "value": 0.15 }
  },
  "chart_indicators": [
    { "type": "MA", "period": "{参数名}" }
  ]
}

## 重要：指标函数 vs 数据源

**指标函数 (func)** - 用于计算技术指标，只能使用以下函数：
ma, ema, wma, rsi, macd, kdj, boll, atr, volume

**数据源 (data)** - K线数据字段，用作指标的输入参数或价格比较：
close, open, high, low, volume, amount

## 规则类型说明

1. **indicator 类型**: 比较两个指标或指标与数值
   - 示例: 5日均线上穿20日均线
   ```json
   {
     "type": "indicator",
     "left": { "func": "ma", "args": ["close", 5] },
     "op": "cross_above",
     "right": { "func": "ma", "args": ["close", 20] }
   }
   ```

2. **price 类型**: 比较价格与数值（如：价格触及布林带下轨）
   ```json
   {
     "type": "price",
     "left": {"data": "close"},
     "op": "<",
     "right": {"func": "boll", "args": ["close", 20], "field": "lower"}
   }
   ```

## 完整示例

**示例1: RSI超卖反弹**
```json
{
  "meta": {"name": "RSI超卖反弹", "description": "RSI低于30后反弹买入", "tags": ["震荡", "RSI"], "source": "agent"},
  "params": {
    "rsi_period": {"value": 14, "min": 2, "max": 50, "step": 1, "label": "RSI周期", "type": "int"},
    "oversold": {"value": 30, "min": 10, "max": 50, "step": 1, "label": "超卖线", "type": "int"}
  },
  "conditions": {
    "buy": {
      "logic": "AND",
      "rules": [
        {
          "type": "indicator",
          "left": {"func": "rsi", "args": ["close", "{rsi_period}"]},
          "op": "<",
          "right": {"value": "{oversold}"}
        }
      ]
    },
    "sell": {
      "logic": "AND",
      "rules": [
        {
          "type": "indicator",
          "left": {"func": "rsi", "args": ["close", "{rsi_period}"]},
          "op": ">",
          "right": {"value": 70}
        }
      ]
    }
  },
  "risk": {"stop_loss": {"type": "fixed", "value": 0.05}, "take_profit": {"type": "fixed", "value": 0.15}},
  "chart_indicators": [{"type": "RSI", "period": "{rsi_period}"}]
}
```

**示例2: MACD+RSI多因子共振**
```json
{
  "meta": {"name": "MACD+RSI共振", "description": "MACD金叉且RSI超卖", "tags": ["多因子", "MACD", "RSI"], "source": "agent"},
  "params": {
    "fast_period": {"value": 12, "min": 5, "max": 50, "step": 1, "label": "快线周期", "type": "int"},
    "slow_period": {"value": 26, "min": 10, "max": 100, "step": 1, "label": "慢线周期", "type": "int"},
    "signal_period": {"value": 9, "min": 5, "max": 30, "step": 1, "label": "信号周期", "type": "int"},
    "rsi_period": {"value": 14, "min": 2, "max": 50, "step": 1, "label": "RSI周期", "type": "int"}
  },
  "conditions": {
    "buy": {
      "logic": "AND",
      "rules": [
        {
          "type": "indicator",
          "left": {"func": "macd", "args": ["close", "{fast_period}", "{slow_period}", "{signal_period}"], "field": "macd"},
          "op": "cross_above",
          "right": {"func": "macd", "args": ["close", "{fast_period}", "{slow_period}", "{signal_period}"], "field": "signal"}
        },
        {
          "type": "indicator",
          "left": {"func": "rsi", "args": ["close", "{rsi_period}"]},
          "op": "<",
          "right": {"value": 30}
        }
      ]
    },
    "sell": {
      "logic": "OR",
      "rules": [
        {
          "type": "indicator",
          "left": {"func": "macd", "args": ["close", "{fast_period}", "{slow_period}", "{signal_period}"], "field": "macd"},
          "op": "cross_below",
          "right": {"func": "macd", "args": ["close", "{fast_period}", "{slow_period}", "{signal_period}"], "field": "signal"}
        },
        {
          "type": "indicator",
          "left": {"func": "rsi", "args": ["close", "{rsi_period}"]},
          "op": ">",
          "right": {"value": 70}
        }
      ]
    }
  },
  "risk": {"stop_loss": {"type": "fixed", "value": 0.05}, "take_profit": {"type": "fixed", "value": 0.15}},
  "chart_indicators": [
    {"type": "MACD", "fast_period": "{fast_period}", "slow_period": "{slow_period}", "signal_period": "{signal_period}"},
    {"type": "RSI", "period": "{rsi_period}"}
  ]
}
```

## 规则清单

- 可用指标函数 (func): ma, ema, wma, rsi, macd, kdj, boll, atr, volume
- 可用数据源 (args 第一个参数或 price 类型的 data): close, open, high, low, volume, amount
- 可用运算符 (op): >, <, >=, <=, ==, !=, cross_above, cross_below
- 可用字段 (field, MACD/KDJ/布林带专用): macd, signal, upper, mid, lower, k, d, j
- params 中定义的参数，在 rules 中用 "{参数名}" 引用（如 "{fast_period}"）

## 重要提醒

1. ❌ **错误**: `{"func": "close"}` - close不是指标函数
2. ✅ **正确**: `{"func": "ma", "args": ["close", 5]}` - close作为数据源传入指标
3. ✅ **正确**: `{"type": "price", "left": {"data": "close"}}` - 使用price类型比较价格
4. params 中每个参数必须有 value/min/max/label，type 用 int 或 float
5. conditions.buy 和 conditions.sell 必须同时存在，每个至少一条规则
6. chart_indicators 列出需要在 K 线图上叠加的指标，参数值用 "{参数名}" 引用
7. 只输出 JSON，不要任何解释文字
8. 参数范围要合理（如均线周期 2-250，RSI 周期 2-50）
9. 止损止盈设为固定百分比（stop_loss: 0.05, take_profit: 0.15 为默认值）"""


class GenerateStrategyRequest(BaseModel):
    prompt: str
    code: str = ""  # 股票代码，用于 LLM 上下文


@router.post('/strategies/generate')
async def generate_strategy(req: GenerateStrategyRequest):
    """Agent 自然语言 → 策略 JSON 生成

    接收用户的自然语言描述（如"5日均线上穿20日均线买入，RSI>80卖出"），
    调用 LLM 生成符合 strategy-schema 格式的策略 JSON。
    """
    if not req.prompt or len(req.prompt.strip()) < 4:
        raise HTTPException(status_code=400, detail='策略描述不能为空或过短')

    try:
        from utils.llm_factory import get_report_llm, llm_json_with_retry, tracked_invoke

        llm = get_report_llm()

        # 构造消息
        code_hint = f"（股票代码: {req.code}）" if req.code else ""
        user_prompt = f"请生成以下策略的 JSON 定义 {code_hint}：\n{req.prompt.strip()}"

        messages = [
            ("system", _STRATEGY_GEN_SYSTEM_PROMPT),
            ("user", user_prompt),
        ]

        _log.info(f"开始生成策略: prompt={req.prompt[:80]}...")

        # 调用 LLM（自动重试 + JSON 提取）
        strategy_json = llm_json_with_retry(
            llm, messages, _log,
            label="策略生成",
            max_retries=3,
        )

        if not strategy_json:
            raise HTTPException(status_code=500, detail='LLM 未返回有效 JSON，请重试或调整描述')

        # 校验生成的 JSON 是否可编译
        try:
            from backtest.json_compiler import compile_strategy
            code_str = json.dumps(strategy_json, ensure_ascii=False)
            compile_strategy(code_str)
        except Exception as e:
            _log.warning(f"策略生成校验失败: {e}")
            raise HTTPException(status_code=400, detail=f'生成的策略 JSON 无效（{e}），请调整描述后重试')

        _log.info(f"策略生成成功: {strategy_json.get('meta', {}).get('name', '未命名')}")
        return {'strategy': strategy_json}

    except HTTPException:
        raise
    except ImportError as e:
        _log.error(f"LLM 模块未安装或配置错误: {e}")
        raise HTTPException(status_code=500, detail=f'LLM 服务未配置: {e}')
    except Exception as e:
        _log.error(f"策略生成异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f'策略生成失败: {e}')


@router.post('/run')
async def run_backtest(req: RunBacktestRequest):
    """执行回测（异步，立即返回 run_id）"""
    strategy = get_strategy(req.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f'策略不存在: {req.strategy_id}')

    config = {
        'code': req.code,
        'strategy_id': req.strategy_id,
        'strategy_name': strategy['name'],
        'params': req.params,
        'start_date': req.start_date,
        'end_date': req.end_date,
        'initial_cash': req.initial_cash,
        'commission': req.commission,
        'min_commission': req.min_commission,
        'stamp_tax': req.stamp_tax,
        'transfer_fee': req.transfer_fee,
        'slippage': req.slippage,
        't_plus_1': req.t_plus_1,
        'lot_size': req.lot_size,
        'adjust_type': req.adjust_type,
        'stop_loss': req.stop_loss,
        'take_profit': req.take_profit,
        'trailing_stop': req.trailing_stop,
        'position_mode': req.position_mode,
        'position_size': req.position_size,
        'asset_type': req.asset_type,
    }

    # 生成 run_id，创建初始记录
    now = datetime.now()
    run_id = f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
    from utils.cache.backtest_db import create_backtest_run
    create_backtest_run(run_id, config)

    # 后台线程执行
    thread = threading.Thread(
        target=_run_backtest_async, args=(run_id, config),
        daemon=True, name=f'backtest-{run_id}'
    )
    thread.start()

    return {'run_id': run_id, 'status': 'pending'}


@router.get('/runs')
async def list_runs(code: Optional[str] = None, strategy_id: Optional[str] = None,
                    page: int = 1, page_size: int = 20):
    """获取回测历史列表（分页）"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    runs, total = get_backtest_runs(
        code=code, strategy_id=strategy_id, limit=page_size, offset=offset
    )
    return {'runs': runs, 'total': total, 'page': page, 'page_size': page_size}


@router.get('/runs/{run_id}')
async def get_run_detail(run_id: str):
    """获取回测详情（含结果和交易记录）"""
    run = get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f'回测记录不存在: {run_id}')

    # 兜底：旧 run 的 stock_name 可能为空，实时补名
    if not run.get('stock_name'):
        run['stock_name'] = _resolve_stock_name(run['code'])

    result = get_backtest_result(run_id)
    trades = get_backtest_trades(run_id)

    # 解析 JSON 字段
    if result:
        for key in ['equity_curve_json', 'drawdown_curve_json', 'monthly_returns_json']:
            if result.get(key):
                try:
                    result[key] = json.loads(result[key])
                except (json.JSONDecodeError, TypeError):
                    pass

    return {
        'run': run,
        'result': result,
        'trades': trades,
    }


@router.get('/runs/{run_id}/progress')
async def get_run_progress(run_id: str):
    """SSE 推送回测进度"""
    async def event_stream():
        # 先检查 run 是否存在
        run = get_backtest_run(run_id)
        if not run:
            yield f'data: {json.dumps({"error": "not_found"})}\n\n'
            return

        # 如果已完成，直接返回
        if run.get('status') in ('completed', 'failed'):
            yield f'data: {json.dumps({"status": run["status"], "pct": 1.0})}\n\n'
            return

        # 持续推送进度
        last_pct = -1
        for _ in range(600):  # 最多等 5 分钟（600 * 500ms）
            # 双保险：每 2 秒查一次 DB 状态（后台线程异常退出时 DB 兜底）
            if _ % 4 == 0:
                db_run = get_backtest_run(run_id)
                if db_run and db_run.get('status') in ('completed', 'failed'):
                    yield f'data: {json.dumps({"status": db_run["status"], "pct": 1.0, "db_fallback": True})}\n\n'
                    return
            progress = _backtest_progress.get(run_id, {})
            status = progress.get('status', 'pending')
            pct = progress.get('pct', 0)

            if pct != last_pct or status in ('completed', 'failed'):
                yield f'data: {json.dumps({"status": status, "pct": pct, "current": progress.get("current", 0), "total": progress.get("total", 0)})}\n\n'
                last_pct = pct

            if status in ('completed', 'failed'):
                break

            await asyncio.sleep(0.5)

        # 超时兜底：最后查一次 DB
        db_run = get_backtest_run(run_id)
        final_status = db_run.get('status') if db_run else 'failed'
        if final_status not in ('completed', 'failed'):
            final_status = 'failed'
        yield f'data: {json.dumps({"status": final_status, "pct": 1.0, "timeout": True})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


def build_run_kline(run_id: str):
    """组装回测结果 K 线数据（K 线 + 成交点 + 本策略指标）。

    复用 data_adapter 的复权逻辑，保证与回测引擎同口径。
    run 不存在返回 None。
    """
    run = get_backtest_run(run_id)
    if not run:
        return None

    code = run['code']
    adjust = run.get('adjust_type') or 'qfq'
    start_date = run['start_date']
    end_date = run['end_date']

    from backtest.data_adapter import _load_stock_daily, _apply_adjustment
    df = _load_stock_daily(code, start_date, end_date, auto_heal=False)
    if adjust in ('qfq', 'hfq') and df is not None and not df.empty:
        df = _apply_adjustment(df, code, adjust)

    kline = []
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            kline.append({
                'date': str(r['trade_date'])[:10],
                'open': round(float(r['open']), 2),
                'high': round(float(r['high']), 2),
                'low': round(float(r['low']), 2),
                'close': round(float(r['close']), 2),
                'volume': int(r['volume']) if pd.notna(r['volume']) else 0,
            })

    trades = []
    for t in get_backtest_trades(run_id):
        trades.append({
            'date': t.get('trade_date'),
            'direction': t.get('direction'),
            'price': t.get('price'),
            'quantity': t.get('quantity'),
            'pnl': t.get('pnl'),
            'pnl_pct': t.get('pnl_pct'),
            'signal_reason': t.get('signal_reason'),
        })

    try:
        from backtest.strategies import get_strategy_class
        params = json.loads(run.get('params_json') or '{}')
        decls = get_strategy_class(run['strategy_id']).get_chart_indicators(params)
    except Exception:
        decls = []
    from server.chart_utils import build_chart_indicators
    indicators = build_chart_indicators(df, decls)

    # 兜底：旧 run 的 stock_name 可能为空，实时查
    name = run.get('stock_name') or ''
    if not name:
        name = _resolve_stock_name(code)

    return {
        'code': code,
        'name': name,
        'adjust_type': adjust,
        'kline': kline,
        'trades': trades,
        'indicators': indicators,
    }


@router.get('/runs/{run_id}/kline')
async def get_run_kline(run_id: str):
    """回测结果 K 线 + 真实买卖点 + 本策略指标"""
    data = build_run_kline(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f'回测记录不存在: {run_id}')
    return data


@router.delete('/runs/{run_id}')
async def delete_run(run_id: str):
    """删除回测记录（级联删除 results + trades）"""
    deleted = delete_backtest_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f'回测记录不存在: {run_id}')
    return {'success': True, 'run_id': run_id}


# ============================================================
# 批量回测 API
# ============================================================

class RunBatchBacktestRequest(BaseModel):
    """批量回测请求"""
    codes: List[str]
    strategy_id: str
    params: Dict[str, Any] = {}
    start_date: str = '2025-01-01'
    end_date: str = '2026-06-01'
    initial_cash: float = 100000
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.001
    t_plus_1: bool = True
    lot_size: int = 100
    adjust_type: str = 'qfq'
    stop_loss: float = 0
    take_profit: float = 0
    trailing_stop: float = 0
    position_mode: str = 'full'
    position_size: float = 1.0
    benchmark: str = '000300'
    concurrency: int = 3


class CreateTestSetRequest(BaseModel):
    name: str
    codes: List[str]
    description: str = ''
    tags: List[str] = []


class UpdateTestSetRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    codes: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class RerunBatchItemRequest(BaseModel):
    code: str


class RerunFailedRequest(BaseModel):
    """批量重试所有失败 item 的请求"""
    concurrency: int = 3


# 单批最多标的数（防 UI 不可读 + 防内存压力）
BACKTEST_BATCH_MAX_CODES = 50


def _get_strategy_default_params(strategy_id: str) -> dict:
    """从策略 JSON 模板提取默认参数值（多策略模式下每个策略用各自的参数）。"""
    s = get_strategy(strategy_id)
    if not s or not s.get('params_schema'):
        return {}
    try:
        schema = json.loads(s['params_schema']) if isinstance(s['params_schema'], str) else s['params_schema']
    except (json.JSONDecodeError, TypeError):
        return {}
    params = {}
    for p in schema:
        if isinstance(p, dict) and 'key' in p:
            params[p['key']] = p.get('default', 0)
    return params



def _lookup_stock_names(codes: list) -> dict:
    """批量查询标的名称（复用 _resolve_stock_name）。"""
    names = {}
    for code in codes:
        name = _resolve_stock_name(code)
        if name:
            names[code] = name
    return names


def _deserialize_json(value) -> dict:
    """反序列化 batch 表的 JSON 字段（str→dict，失败返回空 dict）。
    单个重试 / 批量重试共用，消除重复反序列化代码。
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def _update_batch_progress(batch_id: str, code: str, status: str,
                           current: int, total: int):
    """由 BatchBacktestEngine 回调，更新批次进度"""
    _batch_progress[batch_id] = {
        'current': current,
        'total': total,
        'pct': round(current / total, 4) if total > 0 else 0,
        'status': 'running',
        'last_code': code,
        'last_status': status,
    }


@router.post('/batch-run')
async def run_batch_backtest(req: RunBatchBacktestRequest):
    """提交批量回测（异步，立即返回 batch_id）"""
    # 1. 校验 codes
    if not req.codes:
        raise HTTPException(status_code=400, detail='codes 不能为空')
    if len(req.codes) > BACKTEST_BATCH_MAX_CODES:
        raise HTTPException(
            status_code=400,
            detail=f'单批最多 {BACKTEST_BATCH_MAX_CODES} 个标的'
        )
    strategy = get_strategy(req.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f'策略不存在: {req.strategy_id}')

    # 2. 去重 + 大写 + 6 位补零（仅对纯数字）
    codes = []
    seen = set()
    for c in req.codes:
        c = (c or '').strip().upper()
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        codes.append(c)
    if len(codes) > BACKTEST_BATCH_MAX_CODES:
        codes = codes[:BACKTEST_BATCH_MAX_CODES]
    if not codes:
        raise HTTPException(status_code=400, detail='codes 不能为空')

    # 3. 生成 batch_id + run_ids
    now = datetime.now()
    batch_id = f'BTB-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
    run_ids = [
        f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
        for _ in codes
    ]

    # 4. 落库 batch + items + runs
    config = req.model_dump(exclude={'codes', 'concurrency'})
    create_batch(
        batch_id=batch_id,
        strategy_id=req.strategy_id,
        strategy_name=strategy['name'],
        params=req.params,
        config=config,
        codes=codes,
        initial_cash=req.initial_cash,
        start_date=req.start_date,
        end_date=req.end_date,
    )
    from utils.cache.backtest_db import create_backtest_run
    items_meta = []
    for code, run_id in zip(codes, run_ids):
        asset_type = _auto_detect_asset_type(code)
        per_code_config = {
            **config,
            'code': code,
            'asset_type': asset_type,
            'strategy_id': req.strategy_id,
            'strategy_name': strategy['name'],
            'params': req.params,
        }
        create_backtest_run(run_id, per_code_config)
        items_meta.append({'code': code, 'asset_type': asset_type, 'run_id': run_id})
    create_batch_items(batch_id, items_meta)

    # 5. 启动后台线程
    thread = threading.Thread(
        target=_run_batch_async,
        args=(batch_id, codes, config, run_ids, req.concurrency),
        daemon=True,
        name=f'batch-{batch_id}',
    )
    thread.start()

    return {
        'batch_id': batch_id,
        'items': [
            {'code': c, 'run_id': r, 'status': 'pending'}
            for c, r in zip(codes, run_ids)
        ],
    }


def _run_batch_async(batch_id: str, codes: list, config: dict,
                     run_ids: list, concurrency: int):
    """后台线程执行批量回测"""
    from backtest.batch_engine import BatchBacktestEngine
    engine = BatchBacktestEngine(max_concurrency=5)
    try:
        engine.run(
            codes=codes,
            config={**config, 'concurrency': concurrency},
            batch_id=batch_id,
            run_ids=run_ids,
            progress_callback=_update_batch_progress,
        )
        _batch_progress[batch_id] = {
            **_batch_progress.get(batch_id, {}),
            'status': 'completed',
            'pct': 1.0,
        }
    except Exception as e:
        _log.error(f'批次 {batch_id} 异常: {e}', exc_info=True)
        update_batch(batch_id, status='failed', error_message=str(e)[:500])
        _batch_progress[batch_id] = {
            **_batch_progress.get(batch_id, {}),
            'status': 'failed',
            'pct': 1.0,
        }


@router.get('/batches')
async def list_batches_endpoint(page: int = 1, page_size: int = 20):
    """获取批量回测批次列表（分页）"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    batches, total = list_batches(limit=page_size, offset=offset)
    for b in batches:
        for f in ['params_json', 'config_json', 'codes_json']:
            if b.get(f):
                try:
                    b[f] = json.loads(b[f])
                except (json.JSONDecodeError, TypeError):
                    pass
    return {'batches': batches, 'total': total, 'page': page, 'page_size': page_size}


def _parse_batch_json_fields(batch: dict) -> dict:
    """解析 batch 表中的 JSON 字段（params_json / config_json / codes_json）"""
    if not batch:
        return batch
    for f in ['params_json', 'config_json', 'codes_json']:
        if batch.get(f):
            try:
                batch[f] = json.loads(batch[f])
            except (json.JSONDecodeError, TypeError):
                pass
    return batch


@router.get('/batches/{batch_id}')
async def get_batch_detail(batch_id: str):
    """获取批次详情（含所有 item）"""
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f'批次不存在: {batch_id}')
    _parse_batch_json_fields(batch)
    items = get_batch_items(batch_id)
    # 补全标的名称（新老批次通用）
    codes = [it['code'] for it in items]
    names = _lookup_stock_names(codes)
    # 注入 name 到每个 item
    for it in items:
        it['name'] = names.get(it['code'], '')
    return {'batch': batch, 'items': items}


@router.get('/batches/{batch_id}/progress')
async def stream_batch_progress(batch_id: str):
    """SSE 推送批次进度"""
    async def event_stream():
        batch = get_batch(batch_id)
        if not batch:
            yield f'data: {json.dumps({"error": "not_found"})}\n\n'
            return
        # 已完成的批次直接返回终态
        if batch['status'] in ('completed', 'partial', 'failed'):
            yield f'data: {json.dumps({"status": batch["status"], "pct": 1.0})}\n\n'
            return

        last_pct = -1
        # 最多等 10 分钟（600 * 1s）
        for _ in range(600):
            # 双保险：每 5 秒查一次 DB 状态（后台线程异常退出 / 服务重启后 DB 兜底）
            if _ % 5 == 0:
                db_batch = get_batch(batch_id)
                if db_batch and db_batch['status'] in ('completed', 'partial', 'failed'):
                    yield f'data: {json.dumps({"status": db_batch["status"], "pct": 1.0, "db_fallback": True})}\n\n'
                    return
            progress = _batch_progress.get(batch_id, {})
            pct = progress.get('pct', 0)
            status = progress.get('status', 'running')
            # 只在 pct 或 status 变化时推送
            if pct != last_pct or status in ('completed', 'failed'):
                payload = {
                    'status': status,
                    'pct': pct,
                    'current': progress.get('current', 0),
                    'total': progress.get('total', 0),
                    'last_code': progress.get('last_code'),
                    'last_status': progress.get('last_status'),
                }
                yield f'data: {json.dumps(payload)}\n\n'
                last_pct = pct
            if status in ('completed', 'failed'):
                break
            await asyncio.sleep(1.0)

        # 超时兜底：最后查一次 DB
        db_batch = get_batch(batch_id)
        final_status = db_batch['status'] if db_batch else 'failed'
        if final_status not in ('completed', 'partial', 'failed'):
            final_status = 'failed'
        yield f'data: {json.dumps({"status": final_status, "pct": 1.0, "timeout": True})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.post('/batches/{batch_id}/rerun')
async def rerun_batch_item_endpoint(batch_id: str, req: RerunBatchItemRequest):
    """重跑批次内单个失败 code"""
    item = get_batch_item_by_code(batch_id, req.code)
    if not item:
        raise HTTPException(status_code=404, detail='item 不存在')
    if item['status'] != 'failed':
        raise HTTPException(status_code=400, detail='仅失败 item 可重跑')

    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail='批次不存在')

    # 生成新 run_id
    now = datetime.now()
    new_run_id = f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'

    # 重建 config（用 _deserialize_json 反序列化 batch 表 JSON 字段）
    config = _deserialize_json(batch['config_json'])
    params = _deserialize_json(batch['params_json'])

    config = {
        **config,
        'code': req.code,
        'asset_type': item['asset_type'] or _auto_detect_asset_type(req.code),
        'strategy_id': batch['strategy_id'],
        'strategy_name': batch['strategy_name'] or '',
        'params': params,
        # batch 表独立存储 start_date/end_date，config_json 不一定带，需补齐
        'start_date': config.get('start_date') or batch.get('start_date') or '2025-01-01',
        'end_date': config.get('end_date') or batch.get('end_date') or '2026-06-01',
        'initial_cash': config.get('initial_cash') or batch.get('initial_cash') or 100000,
    }
    from utils.cache.backtest_db import create_backtest_run
    create_backtest_run(new_run_id, config)

    # 标记 item 为 pending + 初始化 batch 进度（触发 SSE 推送）
    update_batch_item(
        batch_id, req.code,
        run_id=new_run_id,
        status='pending',
        error_message=None,
        started_at=now.strftime('%Y-%m-%d %H:%M:%S'),
        completed_at=None,
    )
    # 立即把 batch 状态置为 running（因为已有 pending item），
    # 否则 SSE 端点会因 batch 仍是旧终态而直接返回，无法流式推送进度
    try:
        from backtest.batch_engine import recompute_batch_summary
        recompute_batch_summary(batch_id)
    except Exception as e:
        _log.warning(f'recompute batch {batch_id} (启动) 失败: {e}')
    _batch_progress[batch_id] = {
        **_batch_progress.get(batch_id, {}),
        'current': 0,
        'total': 1,
        'pct': 0,
        'status': 'running',
        'last_code': req.code,
        'last_status': 'pending',
    }

    # 后台执行单 code（不走 BatchEngine，直接 BacktestEngine）
    def _rerun():
        try:
            from backtest.engine import BacktestEngine
            result = BacktestEngine().run(config, run_id=new_run_id)
            update_batch_item(
                batch_id, req.code,
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
            _batch_progress[batch_id] = {
                'current': 1, 'total': 1, 'pct': 1.0,
                'status': 'completed', 'last_code': req.code, 'last_status': 'completed',
            }
        except Exception as e:
            _log.warning(f'重跑 {batch_id} code={req.code} 失败: {e}')
            update_batch_item(
                batch_id, req.code,
                status='failed',
                error_message=str(e)[:500],
                completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            )
            _batch_progress[batch_id] = {
                'current': 1, 'total': 1, 'pct': 1.0,
                'status': 'failed', 'last_code': req.code, 'last_status': 'failed',
            }
        finally:
            # 无论成功失败，都重新计算 batch 汇总（含 status / avg / profit / loss）
            try:
                from backtest.batch_engine import recompute_batch_summary
                recompute_batch_summary(batch_id)
            except Exception as e:
                _log.warning(f'recompute batch {batch_id} 失败: {e}')

    threading.Thread(
        target=_rerun, daemon=True, name=f'rerun-{new_run_id}'
    ).start()
    return {'run_id': new_run_id, 'status': 'pending'}


@router.post('/batches/{batch_id}/rerun-failed')
async def rerun_failed_items_endpoint(batch_id: str, req: RerunFailedRequest = RerunFailedRequest()):
    """批量重试所有失败 item（复用原 batch，成功数据保留）

    与"重新发起整批"的区别：
    - 只对 status='failed' 的 item 生成新 run_id 重跑
    - 已 completed 的 item 原样保留（run_id 不变）
    - 复用 BatchBacktestEngine.run（自带并发 + _aggregate + update_batch），
      末尾会基于全部 item 重算 batch 汇总
    """
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail='批次不存在')

    items = get_batch_items(batch_id)
    failed_items = [it for it in items if it.get('status') == 'failed']
    if not failed_items:
        raise HTTPException(status_code=400, detail='没有失败的 item 可重试')

    # 重建 config/params（复用单个重试的反序列化逻辑）
    config = _deserialize_json(batch.get('config_json'))
    params = _deserialize_json(batch.get('params_json'))

    now = datetime.now()
    started_at = now.strftime('%Y-%m-%d %H:%M:%S')
    from utils.cache.backtest_db import create_backtest_run

    codes, run_ids = [], []
    for it in failed_items:
        code = it['code']
        new_run_id = f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
        per_code_config = {
            **config,
            'code': code,
            'asset_type': it.get('asset_type') or _auto_detect_asset_type(code),
            'strategy_id': batch['strategy_id'],
            'strategy_name': batch.get('strategy_name') or '',
            'params': params,
            'start_date': config.get('start_date') or batch.get('start_date') or '2025-01-01',
            'end_date': config.get('end_date') or batch.get('end_date') or '2026-06-01',
            'initial_cash': config.get('initial_cash') or batch.get('initial_cash') or 100000,
        }
        create_backtest_run(new_run_id, per_code_config)
        # 失败 item 重置为 pending，替换为新 run_id
        update_batch_item(
            batch_id, code,
            run_id=new_run_id,
            status='pending',
            error_message=None,
            started_at=started_at,
            completed_at=None,
            # 清掉上次的指标快照，避免汇总阶段误用旧值
            total_return=None, annual_return=None, sharpe_ratio=None,
            max_drawdown=None, win_rate=None, trade_count=None, final_equity=None,
        )
        codes.append(code)
        run_ids.append(new_run_id)

    # 标记 batch running + 初始化进度（触发 SSE 推送）
    update_batch(batch_id, status='running', started_at=started_at, error_message=None)
    _batch_progress[batch_id] = {
        'current': 0,
        'total': len(codes),
        'pct': 0,
        'status': 'running',
        'last_code': None,
        'last_status': None,
    }

    # 后台复用 BatchBacktestEngine（自带并发 + 进度回调 + _aggregate + update_batch）
    def _rerun_failed():
        from backtest.batch_engine import BatchBacktestEngine
        engine = BatchBacktestEngine(max_concurrency=5)
        try:
            engine.run(
                codes=codes,
                config={**config, 'concurrency': req.concurrency},
                batch_id=batch_id,
                run_ids=run_ids,
                progress_callback=_update_batch_progress,
            )
            _batch_progress[batch_id] = {
                **_batch_progress.get(batch_id, {}),
                'status': 'completed',
                'pct': 1.0,
            }
        except Exception as e:
            _log.error(f'批量重试 {batch_id} 异常: {e}', exc_info=True)
            update_batch(batch_id, status='failed', error_message=str(e)[:500])
            _batch_progress[batch_id] = {
                **_batch_progress.get(batch_id, {}),
                'status': 'failed',
                'pct': 1.0,
            }

    threading.Thread(
        target=_rerun_failed, daemon=True, name=f'rerun-failed-{batch_id}'
    ).start()
    return {
        'count': len(codes),
        'status': 'pending',
        'codes': codes,
        'run_ids': run_ids,
    }


@router.get('/clean-failed')
async def clean_failed_endpoint():
    """SSE 流式清理所有失败的回测记录（单股 + 批量 + 多策略）"""
    async def event_stream():
        stats = {"runs": 0, "batches": 0, "multis": 0}
        yield f'data: {json.dumps({"phase": "scanning", "stats": stats})}\n\n'
        await asyncio.sleep(0.05)

        # 1. 清理失败的单股 runs
        runs, _ = get_backtest_runs(limit=500)
        failed_runs = [r for r in runs if r.get('status') == 'failed']
        stats["runs_total"] = len(failed_runs)
        for i, r in enumerate(failed_runs):
            try:
                delete_backtest_run(r['run_id'])
                stats["runs"] += 1
            except Exception:
                pass
            if i % 5 == 0:
                yield f'data: {json.dumps({"phase": "runs", "current": i+1, "total": len(failed_runs), "stats": stats})}\n\n'
                await asyncio.sleep(0.01)

        yield f'data: {json.dumps({"phase": "runs_done", "stats": stats})}\n\n'
        await asyncio.sleep(0.05)

        # 2. 清理失败的 batches
        batches, _ = list_batches(limit=500)
        failed_batches = [b for b in batches if b.get('status') == 'failed']
        stats["batches_total"] = len(failed_batches)
        for i, b in enumerate(failed_batches):
            try:
                delete_batch(b['batch_id'])
                _batch_progress.pop(b['batch_id'], None)
                stats["batches"] += 1
            except Exception:
                pass
            if i % 3 == 0:
                yield f'data: {json.dumps({"phase": "batches", "current": i+1, "total": len(failed_batches), "stats": stats})}\n\n'
                await asyncio.sleep(0.01)

        yield f'data: {json.dumps({"phase": "batches_done", "stats": stats})}\n\n'
        await asyncio.sleep(0.05)

        # 3. 清理失败的 multis
        multis, _ = list_multi(limit=500)
        failed_multis = [m for m in multis if m.get('status') == 'failed']
        stats["multis_total"] = len(failed_multis)
        for i, m in enumerate(failed_multis):
            try:
                delete_multi(m['multi_id'])
                _multi_progress.pop(m['multi_id'], None)
                stats["multis"] += 1
            except Exception:
                pass
            yield f'data: {json.dumps({"phase": "multis", "current": i+1, "total": len(failed_multis), "stats": stats})}\n\n'
            await asyncio.sleep(0.01)

        # 完成
        total = stats["runs"] + stats["batches"] + stats["multis"]
        yield f'data: {json.dumps({"phase": "done", "total_cleaned": total, "stats": stats})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.delete('/batches/{batch_id}')
async def delete_batch_endpoint(batch_id: str):
    """删除批次（连带明细）"""
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail='批次不存在')
    delete_batch(batch_id)
    _batch_progress.pop(batch_id, None)
    return {'success': True}


# ============================================================
# 多策略批量回测（M 策略 × N 标的）
# ============================================================

class RunMultiBacktestRequest(BaseModel):
    """多策略批量回测请求"""
    strategy_ids: List[str]
    codes: List[str]
    params: Dict[str, Any] = {}
    start_date: str = '2025-01-01'
    end_date: str = '2026-06-01'
    initial_cash: float = 100000
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.001
    t_plus_1: bool = True
    lot_size: int = 100
    adjust_type: str = 'qfq'
    stop_loss: float = 0
    take_profit: float = 0
    trailing_stop: float = 0
    position_mode: str = 'full'
    position_size: float = 1.0
    benchmark: str = '000300'
    concurrency: int = 3

# 多策略全局进度缓存
_multi_progress: Dict[str, dict] = {}


@router.post('/multi-run')
async def run_multi_backtest(req: RunMultiBacktestRequest):
    """提交多策略批量回测（异步，立即返回 multi_id）"""
    # 1. 校验
    if not req.strategy_ids or len(req.strategy_ids) < 2:
        raise HTTPException(status_code=400, detail='至少选择 2 个策略')
    if len(req.strategy_ids) > 5:
        raise HTTPException(status_code=400, detail='最多 5 个策略')
    if not req.codes:
        raise HTTPException(status_code=400, detail='codes 不能为空')
    if len(req.codes) > BACKTEST_BATCH_MAX_CODES:
        raise HTTPException(status_code=400,
                            detail=f'单批最多 {BACKTEST_BATCH_MAX_CODES} 个标的')

    # 校验策略存在 + 获取名称
    strategy_names = []
    for sid in req.strategy_ids:
        s = get_strategy(sid)
        if not s:
            raise HTTPException(status_code=404, detail=f'策略不存在: {sid}')
        strategy_names.append(s['name'])

    # 去重 codes
    codes = []
    seen = set()
    for c in req.codes:
        c = (c or '').strip().upper()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    if len(codes) > BACKTEST_BATCH_MAX_CODES:
        codes = codes[:BACKTEST_BATCH_MAX_CODES]
    if not codes:
        raise HTTPException(status_code=400, detail='codes 不能为空')

    # 2. 创建 multi 主记录
    now = datetime.now()
    multi_id = f'MUL-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
    create_multi(
        multi_id=multi_id,
        strategy_ids=req.strategy_ids,
        strategy_names=strategy_names,
        codes=codes,
        initial_cash=req.initial_cash,
        start_date=req.start_date,
        end_date=req.end_date,
    )

    # 3. 对每个策略创建独立 batch（每个策略用自己的默认参数，避免参数混用）
    base_config = req.model_dump(exclude={'strategy_ids', 'codes', 'concurrency', 'params'})
    all_items = []
    batches_info = []

    for sid, sname in zip(req.strategy_ids, strategy_names):
        # 每个策略使用自己的默认参数（从 JSON 模板提取），不共享前端表单参数
        strat_params = _get_strategy_default_params(sid)
        batch_id = f'BTB-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}'
        run_ids = [f'BT-{now.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6].upper()}' for _ in codes]

        create_batch(
            batch_id=batch_id,
            strategy_id=sid,
            strategy_name=sname,
            params=strat_params,
            config={**base_config, 'strategy_id': sid},
            codes=codes,
            initial_cash=req.initial_cash,
            start_date=req.start_date,
            end_date=req.end_date,
        )

        from utils.cache.backtest_db import create_backtest_run
        items_meta = []
        for code, run_id in zip(codes, run_ids):
            asset_type = _auto_detect_asset_type(code)
            per_code_config = {
                **base_config,
                'code': code, 'asset_type': asset_type,
                'strategy_id': sid, 'strategy_name': sname,
                'params': strat_params,
            }
            create_backtest_run(run_id, per_code_config)
            items_meta.append({'code': code, 'asset_type': asset_type, 'run_id': run_id})
            all_items.append({
                'strategy_id': sid, 'code': code,
                'batch_id': batch_id, 'run_id': run_id,
            })
        create_batch_items(batch_id, items_meta)
        batches_info.append({'strategy_id': sid, 'batch_id': batch_id})

    # 4. 创建 multi items
    create_multi_items(multi_id, all_items)

    # 5. 启动后台线程：对每个 batch 并发执行
    thread = threading.Thread(
        target=_run_multi_async,
        args=(multi_id, req.strategy_ids, batches_info, codes, base_config, req.concurrency),
        daemon=True, name=f'multi-{multi_id}',
    )
    thread.start()

    return {
        'multi_id': multi_id,
        'batches': batches_info,
        'status': 'pending',
    }


def _run_multi_async(multi_id: str, strategy_ids: list, batches_info: list,
                     codes: list, base_config: dict, concurrency: int):
    """后台线程：对每个策略的 batch 并发执行"""
    from backtest.batch_engine import BatchBacktestEngine
    from concurrent.futures import ThreadPoolExecutor, as_completed

    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    update_multi(multi_id, status='running', started_at=started_at)

    total_batches = len(batches_info)
    completed_batches = 0
    # 多策略模式下每 batch 内并发降到 2（避免数据源限频导致大量失败）
    inner_concurrency = min(concurrency, 2)

    # 初始化进度（让前端 SSE 立即看到 0%）
    _multi_progress[multi_id] = {
        'completed': 0, 'total': total_batches, 'pct': 0.0, 'status': 'running',
    }

    def _run_one_batch(bi):
        engine = BatchBacktestEngine(max_concurrency=inner_concurrency)
        batch_id = bi['batch_id']
        run_ids = []
        for it in get_batch_items(batch_id):
            if it.get('run_id'):
                run_ids.append(it['run_id'])
        batch_codes = [it['code'] for it in get_batch_items(batch_id)]
        # 每个策略用自己的默认参数，避免不同策略参数混用
        strat_params = _get_strategy_default_params(bi['strategy_id'])
        return engine.run(
            codes=batch_codes,
            config={**base_config, 'strategy_id': bi['strategy_id'],
                    'params': strat_params, 'concurrency': inner_concurrency},
            batch_id=batch_id,
            run_ids=run_ids,
            progress_callback=None,
        )

    try:
        # 多策略模式：batch 串行执行（避免数据源限频 + CPU 争抢导致大量失败）
        max_m = 1
        with ThreadPoolExecutor(max_workers=max_m) as executor:
            future_map = {
                executor.submit(_run_one_batch, bi): bi
                for bi in batches_info
            }
            for future in as_completed(future_map):
                bi = future_map[future]
                try:
                    result = future.result()
                    # 同步 batch 结果到 multi_items
                    for it in result.get('items', []):
                        update_multi_item(
                            multi_id, bi['strategy_id'], it['code'],
                            status=it.get('status', 'completed'),
                            total_return=it.get('total_return'),
                            annual_return=it.get('annual_return'),
                            sharpe_ratio=it.get('sharpe_ratio'),
                            max_drawdown=it.get('max_drawdown'),
                            win_rate=it.get('win_rate'),
                            trade_count=it.get('trade_count'),
                            completed_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        )
                except Exception as e:
                    _log.warning(f'multi {multi_id} 策略 {bi["strategy_id"]} 失败: {e}')
                    # 标记该策略下所有 item 为 failed
                    for it in get_batch_items(bi['batch_id']):
                        update_multi_item(
                            multi_id, bi['strategy_id'], it['code'],
                            status='failed',
                        )
                completed_batches += 1
                _multi_progress[multi_id] = {
                    'completed': completed_batches,
                    'total': total_batches,
                    'pct': round(completed_batches / total_batches, 4),
                    'status': 'running',
                }

        # 全部完成
        completed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        duration = round((datetime.now() - datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S')).total_seconds(), 2)
        update_multi(multi_id, status='completed', completed_at=completed_at,
                     duration_seconds=duration)
        _multi_progress[multi_id] = {**_multi_progress.get(multi_id, {}),
                                      'status': 'completed', 'pct': 1.0}

    except Exception as e:
        _log.error(f'multi {multi_id} 异常: {e}', exc_info=True)
        update_multi(multi_id, status='failed', error_message=str(e)[:500])
        _multi_progress[multi_id] = {**_multi_progress.get(multi_id, {}),
                                      'status': 'failed', 'pct': 1.0}


@router.get('/multi/{multi_id}')
async def get_multi_detail(multi_id: str):
    """获取多策略批次详情（热力图数据 + 各策略 batch 引用）"""
    multi = get_multi(multi_id)
    if not multi:
        raise HTTPException(status_code=404, detail=f'批次不存在: {multi_id}')
    for f in ['strategy_ids_json', 'strategy_names_json', 'codes_json']:
        if multi.get(f):
            try:
                multi[f] = json.loads(multi[f])
            except (json.JSONDecodeError, TypeError):
                pass
    items = get_multi_items(multi_id)
    # 补全标的名称 + 计算超额收益
    codes = list(set(it['code'] for it in items))
    code_names = _lookup_stock_names(codes)
    for it in items:
        it['code_name'] = code_names.get(it['code'], '')
        # 从 backtest_results 补 max_consecutive_losses
        if it.get('run_id') and it.get('status') == 'completed':
            try:
                result = get_backtest_result(it['run_id'])
                if result:
                    it['max_consecutive_losses'] = result.get('max_consecutive_losses', 0)
            except Exception:
                pass
    return {'multi': multi, 'items': items, 'code_names': code_names}


@router.get('/multi/{multi_id}/progress')
async def stream_multi_progress(multi_id: str):
    """SSE 推送多策略批次进度"""
    async def event_stream():
        multi = get_multi(multi_id)
        if not multi:
            yield f'data: {json.dumps({"error": "not_found"})}\n\n'
            return
        if multi['status'] in ('completed', 'partial', 'failed'):
            yield f'data: {json.dumps({"status": multi["status"], "pct": 1.0})}\n\n'
            return

        last_pct = -1
        for _ in range(600):
            # 双保险：每 5 秒查一次 DB 状态（后台线程异常退出 / 服务重启后 DB 兜底）
            if _ % 5 == 0:
                db_multi = get_multi(multi_id)
                if db_multi and db_multi['status'] in ('completed', 'partial', 'failed'):
                    yield f'data: {json.dumps({"status": db_multi["status"], "pct": 1.0, "db_fallback": True})}\n\n'
                    return
            progress = _multi_progress.get(multi_id, {})
            pct = progress.get('pct', 0)
            status = progress.get('status', 'running')
            if pct != last_pct or status in ('completed', 'failed'):
                yield f'data: {json.dumps({"status": status, "pct": pct, "completed": progress.get("completed", 0), "total": progress.get("total", 0)})}\n\n'
                last_pct = pct
            if status in ('completed', 'failed'):
                break
            await asyncio.sleep(1.0)

        # 超时兜底：最后查一次 DB
        db_multi = get_multi(multi_id)
        final_status = db_multi['status'] if db_multi else 'failed'
        if final_status not in ('completed', 'partial', 'failed'):
            final_status = 'failed'
        yield f'data: {json.dumps({"status": final_status, "pct": 1.0, "timeout": True})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@router.get('/multi-list')
async def list_multi_endpoint(page: int = 1, page_size: int = 20):
    """获取多策略批次历史列表（分页）"""
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    multis, total = list_multi(limit=page_size, offset=offset)
    for m in multis:
        for f in ['strategy_ids_json', 'strategy_names_json', 'codes_json']:
            if m.get(f):
                try:
                    m[f] = json.loads(m[f])
                except (json.JSONDecodeError, TypeError):
                    pass
    return {'multis': multis, 'total': total, 'page': page, 'page_size': page_size}


@router.delete('/multi/{multi_id}')
async def delete_multi_endpoint(multi_id: str):
    """删除多策略批次（级联删除所有关联 batch）"""
    multi = get_multi(multi_id)
    if not multi:
        raise HTTPException(status_code=404, detail=f'批次不存在: {multi_id}')
    delete_multi(multi_id)
    _multi_progress.pop(multi_id, None)
    _batch_progress.pop(multi_id, None)
    return {'success': True}


# ============================================================
# 测试集 CRUD
# ============================================================

@router.get('/test-sets')
async def list_test_sets_endpoint():
    """获取全部测试集"""
    sets = list_test_sets()
    for s in sets:
        for f in ['codes_json', 'tags_json']:
            if s.get(f):
                try:
                    s[f] = json.loads(s[f])
                except (json.JSONDecodeError, TypeError):
                    pass
    return {'test_sets': sets}


@router.post('/test-sets')
async def create_test_set_endpoint(req: CreateTestSetRequest):
    """创建测试集"""
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail='名称必填')
    if len(req.codes) == 0 or len(req.codes) > 50:
        raise HTTPException(status_code=400, detail='codes 数量需在 1-50 之间')

    # 去重 + 大写 + 6 位补零
    codes = []
    seen = set()
    for c in req.codes:
        c = (c or '').strip().upper()
        if not c or c in seen:
            continue
        seen.add(c)
        codes.append(c)
    if not codes:
        raise HTTPException(status_code=400, detail='codes 不能为空')

    try:
        existing = get_test_set_by_name(req.name.strip())
        if existing:
            raise HTTPException(status_code=400, detail=f'名称已存在: {req.name}')
        ts = create_test_set(req.name.strip(), codes, req.description, req.tags)
        # 解析返回的 JSON 字段
        for f in ['codes_json', 'tags_json']:
            if ts and ts.get(f):
                try:
                    ts[f] = json.loads(ts[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        return ts
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/test-sets/{test_set_id}')
async def get_test_set_endpoint(test_set_id: int):
    """获取单个测试集"""
    ts = get_test_set(test_set_id)
    if not ts:
        raise HTTPException(status_code=404, detail='测试集不存在')
    for f in ['codes_json', 'tags_json']:
        if ts.get(f):
            try:
                ts[f] = json.loads(ts[f])
            except (json.JSONDecodeError, TypeError):
                pass
    return ts


@router.put('/test-sets/{test_set_id}')
async def update_test_set_endpoint(test_set_id: int, req: UpdateTestSetRequest):
    """更新测试集"""
    ts = get_test_set(test_set_id)
    if not ts:
        raise HTTPException(status_code=404, detail='测试集不存在')

    kwargs: Dict[str, Any] = {}
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(status_code=400, detail='名称不能为空')
        dup = get_test_set_by_name(req.name.strip())
        if dup and dup['id'] != test_set_id:
            raise HTTPException(status_code=400, detail='名称已存在')
        kwargs['name'] = req.name.strip()
    if req.description is not None:
        kwargs['description'] = req.description
    if req.codes is not None:
        if len(req.codes) == 0 or len(req.codes) > 50:
            raise HTTPException(status_code=400, detail='codes 数量需在 1-50 之间')
        # 去重 + 大写
        codes = []
        seen = set()
        for c in req.codes:
            c = (c or '').strip().upper()
            if not c or c in seen:
                continue
            seen.add(c)
            codes.append(c)
        kwargs['codes'] = codes
    if req.tags is not None:
        kwargs['tags'] = req.tags

    update_test_set(test_set_id, **kwargs)
    updated = get_test_set(test_set_id)
    for f in ['codes_json', 'tags_json']:
        if updated.get(f):
            try:
                updated[f] = json.loads(updated[f])
            except (json.JSONDecodeError, TypeError):
                pass
    return updated


@router.delete('/test-sets/{test_set_id}')
async def delete_test_set_endpoint(test_set_id: int):
    """删除测试集"""
    ok = delete_test_set(test_set_id)
    if not ok:
        raise HTTPException(status_code=404, detail='测试集不存在')
    return {'success': True}
