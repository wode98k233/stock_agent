# -*- coding: utf-8 -*-
"""
数据质量校验

对数据源返回的数据进行结构化校验：
- 空数据检测
- 列名校验
- 数据类型校验
- 异常值检测
"""
import logging
from typing import List, Optional, Set

import pandas as pd

logger = logging.getLogger("radar.fetcher.validator")


class DataValidationError(Exception):
    """数据校验错误"""
    pass


class DataValidator:
    """
    数据校验器

    使用方式：
        validator = DataValidator()
        validator.validate_dataframe(df, required_columns=['日期', '收盘'])
    """

    # 历史K线必要列（使用中文作为标准名称）
    HIST_REQUIRED_COLUMNS = {
        '日期', '开盘', '收盘', '最高', '最低', '成交量'
    }

    # 实时行情必要列（使用中文作为标准名称）
    REALTIME_REQUIRED_COLUMNS = {
        '代码', '最新价'
    }

    # 全市场行情必要列（使用中文作为标准名称）
    SPOT_REQUIRED_COLUMNS = {
        '代码', '最新价'
    }

    # 中英文列名映射
    COLUMN_ALIASES = {
        '日期': {'日期', 'date'},
        '开盘': {'开盘', 'open'},
        '收盘': {'收盘', 'close'},
        '最高': {'最高', 'high'},
        '最低': {'最低', 'low'},
        '成交量': {'成交量', 'volume'},
        '代码': {'代码', 'code'},
        '最新价': {'最新价', 'price'},
    }

    @classmethod
    def validate_dataframe(
        cls,
        df: pd.DataFrame,
        required_columns: Optional[Set[str]] = None,
        min_rows: int = 1,
        source_name: str = "unknown",
    ) -> bool:
        """
        校验 DataFrame

        Args:
            df: 要校验的 DataFrame
            required_columns: 必要列名集合（支持中英文别名）
            min_rows: 最小行数
            source_name: 数据源名称（用于日志）

        Returns:
            True 表示校验通过

        Raises:
            DataValidationError: 校验失败
        """
        # 检查是否为 None
        if df is None:
            raise DataValidationError(f"[{source_name}] 返回 None")

        # 检查是否为空
        if df.empty:
            raise DataValidationError(f"[{source_name}] 返回空 DataFrame")

        # 检查行数
        if len(df) < min_rows:
            raise DataValidationError(
                f"[{source_name}] 行数不足: {len(df)} < {min_rows}"
            )

        # 检查必要列（支持中英文别名）
        if required_columns:
            actual_columns = set(df.columns)

            # 检查每个必要列是否有匹配的别名
            missing_columns = set()
            for required_col in required_columns:
                # 查找该列的所有别名
                aliases = cls.COLUMN_ALIASES.get(required_col, {required_col})
                # 检查是否有任何别名存在
                if not aliases.intersection(actual_columns):
                    missing_columns.add(required_col)

            if missing_columns:
                raise DataValidationError(
                    f"[{source_name}] 缺少必要列: {missing_columns}"
                )

        return True

    @classmethod
    def validate_hist_data(cls, df: pd.DataFrame, source_name: str = "unknown") -> bool:
        """
        校验历史K线数据

        Args:
            df: 历史K线 DataFrame
            source_name: 数据源名称

        Returns:
            True 表示校验通过
        """
        return cls.validate_dataframe(
            df,
            required_columns=cls.HIST_REQUIRED_COLUMNS,
            min_rows=1,
            source_name=source_name,
        )

    @classmethod
    def validate_realtime_data(cls, df: pd.DataFrame, source_name: str = "unknown") -> bool:
        """
        校验实时行情数据

        Args:
            df: 实时行情 DataFrame
            source_name: 数据源名称

        Returns:
            True 表示校验通过
        """
        return cls.validate_dataframe(
            df,
            required_columns=cls.REALTIME_REQUIRED_COLUMNS,
            min_rows=1,
            source_name=source_name,
        )

    @classmethod
    def validate_spot_data(cls, df: pd.DataFrame, source_name: str = "unknown") -> bool:
        """
        校验全市场行情数据

        Args:
            df: 全市场行情 DataFrame
            source_name: 数据源名称

        Returns:
            True 表示校验通过
        """
        return cls.validate_dataframe(
            df,
            required_columns=cls.SPOT_REQUIRED_COLUMNS,
            min_rows=10,  # 全市场至少应该有10只股票
            source_name=source_name,
        )

    @classmethod
    def check_price_anomaly(cls, df: pd.DataFrame, source_name: str = "unknown") -> bool:
        """
        检测价格异常

        Args:
            df: 包含价格列的 DataFrame
            source_name: 数据源名称

        Returns:
            True 表示无异常
        """
        price_columns = ['最新价', '收盘', '开盘', '最高', '最低']
        available_cols = [c for c in price_columns if c in df.columns]

        for col in available_cols:
            # 检查负数价格
            if (df[col] < 0).any():
                logger.warning(f"[{source_name}] {col} 存在负数价格")
                return False

            # 检查极端值（价格超过100万可能是错误数据）
            if (df[col] > 1000000).any():
                logger.warning(f"[{source_name}] {col} 存在极端值")
                return False

        return True

    @classmethod
    def check_volume_anomaly(cls, df: pd.DataFrame, source_name: str = "unknown") -> bool:
        """
        检测成交量异常

        Args:
            df: 包含成交量列的 DataFrame
            source_name: 数据源名称

        Returns:
            True 表示无异常
        """
        if '成交量' not in df.columns:
            return True

        # 检查负数成交量
        if (df['成交量'] < 0).any():
            logger.warning(f"[{source_name}] 存在负数成交量")
            return False

        return True


def validate_and_raise(
    df: pd.DataFrame,
    data_type: str = "hist",
    source_name: str = "unknown",
) -> bool:
    """
    校验数据并抛出异常

    Args:
        df: 要校验的 DataFrame
        data_type: 数据类型 (hist/realtime/spot)
        source_name: 数据源名称

    Returns:
        True 表示校验通过

    Raises:
        DataValidationError: 校验失败
    """
    validator = DataValidator()

    if data_type == "hist":
        return validator.validate_hist_data(df, source_name)
    elif data_type == "realtime":
        return validator.validate_realtime_data(df, source_name)
    elif data_type == "spot":
        return validator.validate_spot_data(df, source_name)
    else:
        return validator.validate_dataframe(df, source_name=source_name)


def safe_validate(
    df: pd.DataFrame,
    data_type: str = "hist",
    source_name: str = "unknown",
) -> bool:
    """
    安全校验（不抛出异常）

    Args:
        df: 要校验的 DataFrame
        data_type: 数据类型 (hist/realtime/spot)
        source_name: 数据源名称

    Returns:
        True 表示校验通过，False 表示校验失败
    """
    try:
        return validate_and_raise(df, data_type, source_name)
    except DataValidationError as e:
        logger.warning(f"数据校验失败: {e}")
        return False
