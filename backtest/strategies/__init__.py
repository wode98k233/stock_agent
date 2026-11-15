"""内置策略注册

策略来源有两种：
1. JSON 模板 — 从 strategies 表的 code 字段加载，通过 json_compiler 编译
2. Python 类 — 用 @register_strategy 装饰器注册（旧方式，逐步淘汰）
"""
import json
import logging
from typing import Dict, Type
import backtrader as bt

logger = logging.getLogger(__name__)

# 策略注册表
_STRATEGY_REGISTRY: Dict[str, Type] = {}

# 标记是否已从数据库加载 JSON 策略
_json_strategies_loaded = False


def get_strategy_class(strategy_id: str) -> Type:
    """获取策略类"""
    # 首次调用时自动加载 JSON 策略
    if not _json_strategies_loaded:
        load_json_strategies()
    if strategy_id not in _STRATEGY_REGISTRY:
        # 进程重启后注册表里只剩 strategies 表里的策略；策略研发中途的策略（sr_<指纹>）
        # 只在 strategy_iter 里，这里按需补编译一次，否则「换个标的验证」会报未知策略。
        ensure_json_strategy(strategy_id)
    if strategy_id not in _STRATEGY_REGISTRY:
        raise ValueError(f'未知策略: {strategy_id}，可用: {list(_STRATEGY_REGISTRY.keys())}')
    return _STRATEGY_REGISTRY[strategy_id]


def ensure_json_strategy(strategy_id: str) -> bool:
    """按 strategy_id 把还没进注册表的 JSON 策略补编译进内存注册表。

    来源优先级：
    1) strategies 表的 code（已采纳/已保存的策略）
    2) strategy_iter.strategy_json（策略研发中途的策略：id 形如 sr_<指纹>，从未落 strategies 表）

    Returns:
        是否已可用（True 表示注册表里现在有这个 id）
    """
    if not strategy_id:
        return False
    if strategy_id in _STRATEGY_REGISTRY:
        return True
    code = ''
    try:
        from utils.cache.backtest_db import get_strategies, get_strategy_iter_by_hash

        for s in get_strategies():
            if s.get('strategy_id') == strategy_id:
                code = str(s.get('code') or '').strip()
                break
        if not code.startswith('{'):
            code = ''
            h = strategy_id[3:] if strategy_id.startswith('sr_') else strategy_id
            row = get_strategy_iter_by_hash(h)
            code = str((row or {}).get('strategy_json') or '').strip()
        if not code.startswith('{'):
            return False
        from backtest.json_compiler import compile_strategy

        _STRATEGY_REGISTRY[strategy_id] = compile_strategy(code)
        logger.info(f'按需补编译 JSON 策略: {strategy_id}')
        return True
    except Exception as e:
        logger.warning(f'按需补编译 JSON 策略失败 ({strategy_id}): {e}')
        return False


def get_all_strategies() -> Dict[str, Type]:
    """获取所有已注册策略"""
    if not _json_strategies_loaded:
        load_json_strategies()
    return _STRATEGY_REGISTRY.copy()


def load_json_strategies(force: bool = False):
    """从 strategies 表加载 JSON 策略到注册表

    读取 code 字段，如果是 JSON 则编译为 Backtrader 策略类。
    内置策略的 code 字段由 init_backtest_tables() 写入。
    JSON 策略会覆盖同 strategy_id 的 Python 注册类。

    Args:
        force: 强制重新加载（用户策略更新后使用）
    """
    global _json_strategies_loaded
    if _json_strategies_loaded and not force:
        return
    _json_strategies_loaded = True

    try:
        from utils.cache.backtest_db import get_strategies
        from backtest.json_compiler import compile_strategy

        strategies = get_strategies()
        for s in strategies:
            code = (s.get('code') or '').strip()
            if not code or not code.startswith('{'):
                continue
            sid = s['strategy_id']
            try:
                strategy_cls = compile_strategy(code)
                _STRATEGY_REGISTRY[sid] = strategy_cls  # 允许覆盖 Python 版本
                logger.info(f'已加载 JSON 策略: {sid}')
            except Exception as e:
                logger.warning(f'加载 JSON 策略失败 ({sid}): {e}')
    except Exception as e:
        logger.warning(f'从数据库加载 JSON 策略失败: {e}')


# 策略类通过 load_json_strategies() 从 strategies 表的 code 字段（JSON）编译加载。
# 内置 JSON 模板在 init_backtest_tables() 时自动写入数据库。
