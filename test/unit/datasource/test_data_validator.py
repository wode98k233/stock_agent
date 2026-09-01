# -*- coding: utf-8 -*-
"""数据校验器单元测试。"""
import os
import sys

import pytest
import pandas as pd

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TestDataValidator:
    """数据校验器测试"""

    def test_validate_dataframe_none(self):
        """测试 None 输入"""
        from tools.fetcher.data_validator import DataValidator, DataValidationError

        with pytest.raises(DataValidationError, match="返回 None"):
            DataValidator.validate_dataframe(None)

    def test_validate_dataframe_empty(self):
        """测试空 DataFrame"""
        from tools.fetcher.data_validator import DataValidator, DataValidationError

        df = pd.DataFrame()
        with pytest.raises(DataValidationError, match="返回空 DataFrame"):
            DataValidator.validate_dataframe(df)

    def test_validate_dataframe_insufficient_rows(self):
        """测试行数不足"""
        from tools.fetcher.data_validator import DataValidator, DataValidationError

        df = pd.DataFrame({"a": [1]})
        with pytest.raises(DataValidationError, match="行数不足"):
            DataValidator.validate_dataframe(df, min_rows=5)

    def test_validate_dataframe_missing_columns(self):
        """测试缺少必要列"""
        from tools.fetcher.data_validator import DataValidator, DataValidationError

        df = pd.DataFrame({"a": [1, 2, 3]})
        with pytest.raises(DataValidationError, match="缺少必要列"):
            DataValidator.validate_dataframe(
                df,
                required_columns={"a", "b", "c"},
            )

    def test_validate_dataframe_success(self):
        """测试校验成功"""
        from tools.fetcher.data_validator import DataValidator

        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        assert DataValidator.validate_dataframe(
            df,
            required_columns={"a", "b"},
        ) is True

    def test_validate_hist_data(self):
        """测试历史K线校验"""
        from tools.fetcher.data_validator import DataValidator

        df = pd.DataFrame({
            "日期": ["2026-01-01", "2026-01-02"],
            "开盘": [100.0, 101.0],
            "收盘": [101.0, 102.0],
            "最高": [102.0, 103.0],
            "最低": [99.0, 100.0],
            "成交量": [1000, 2000],
        })
        assert DataValidator.validate_hist_data(df) is True

    def test_validate_realtime_data(self):
        """测试实时行情校验"""
        from tools.fetcher.data_validator import DataValidator

        df = pd.DataFrame({
            "代码": ["600519"],
            "最新价": [1688.0],
        })
        assert DataValidator.validate_realtime_data(df) is True

    def test_validate_spot_data(self):
        """测试全市场行情校验"""
        from tools.fetcher.data_validator import DataValidator

        # 创建足够多的行
        df = pd.DataFrame({
            "代码": [f"{i:06d}" for i in range(100)],
            "最新价": [10.0 + i for i in range(100)],
        })
        assert DataValidator.validate_spot_data(df) is True

    def test_check_price_anomaly(self):
        """测试价格异常检测"""
        from tools.fetcher.data_validator import DataValidator

        # 正常数据
        df_normal = pd.DataFrame({"最新价": [10.0, 20.0, 30.0]})
        assert DataValidator.check_price_anomaly(df_normal) is True

        # 负数价格
        df_negative = pd.DataFrame({"最新价": [10.0, -20.0, 30.0]})
        assert DataValidator.check_price_anomaly(df_negative) is False

        # 极端值
        df_extreme = pd.DataFrame({"最新价": [10.0, 2000000.0, 30.0]})
        assert DataValidator.check_price_anomaly(df_extreme) is False

    def test_check_volume_anomaly(self):
        """测试成交量异常检测"""
        from tools.fetcher.data_validator import DataValidator

        # 正常数据
        df_normal = pd.DataFrame({"成交量": [100, 200, 300]})
        assert DataValidator.check_volume_anomaly(df_normal) is True

        # 负数成交量
        df_negative = pd.DataFrame({"成交量": [100, -200, 300]})
        assert DataValidator.check_volume_anomaly(df_negative) is False


class TestSafeValidate:
    """安全校验函数测试"""

    def test_safe_validate_success(self):
        """测试安全校验成功"""
        from tools.fetcher.data_validator import safe_validate

        df = pd.DataFrame({
            "代码": ["600519"],
            "最新价": [1688.0],
        })
        assert safe_validate(df, data_type="realtime") is True

    def test_safe_validate_failure(self):
        """测试安全校验失败"""
        from tools.fetcher.data_validator import safe_validate

        df = pd.DataFrame()
        assert safe_validate(df, data_type="hist") is False

    def test_safe_validate_unknown_type(self):
        """测试未知数据类型"""
        from tools.fetcher.data_validator import safe_validate

        df = pd.DataFrame({"a": [1, 2, 3]})
        assert safe_validate(df, data_type="unknown") is True
