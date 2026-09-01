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
        raise ValueError(f'未知策略: {strategy_id}，可用: {list(_STRATEGY_REGISTRY.keys())}')
    return _STRATEGY_REGISTRY[strategy_id]


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
