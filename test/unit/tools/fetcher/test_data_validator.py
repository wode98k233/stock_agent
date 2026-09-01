"""
测试目标: tools/fetcher/data_validator.py
覆盖范围:
  - DataValidator.validate_dataframe: None/空/行数不足/列名校验/别名匹配
  - validate_hist_data / validate_realtime_data / validate_spot_data
  - check_price_anomaly: 负数/极端值/无价格列
  - check_volume_anomaly: 负数/无列
  - validate_and_raise: 各 data_type 分支
  - safe_validate: 异常捕获
Mock 策略: 纯 pandas 逻辑，无需 mock
"""
import pytest
import pandas as pd
from tools.fetcher.data_validator import (
    DataValidator, DataValidationError, validate_and_raise, safe_validate
)


class TestValidateDataframe:
    """validate_dataframe: DataFrame 校验"""

    def test_none_raises(self):
        with pytest.raises(DataValidationError, match="None"):
            DataValidator.validate_dataframe(None)

    def test_empty_raises(self):
        df = pd.DataFrame()
        with pytest.raises(DataValidationError, match="空"):
            DataValidator.validate_dataframe(df)

    def test_insufficient_rows_raises(self):
        df = pd.DataFrame({"a": [1]})
        with pytest.raises(DataValidationError, match="行数不足"):
            DataValidator.validate_dataframe(df, min_rows=5)

    def test_valid_passes(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        assert DataValidator.validate_dataframe(df) is True

    def test_required_columns_present(self):
        df = pd.DataFrame({"日期": ["2026-01-01"], "收盘": [100]})
        assert DataValidator.validate_dataframe(df, required_columns={"日期", "收盘"}) is True

    def test_required_columns_missing_raises(self):
        df = pd.DataFrame({"日期": ["2026-01-01"]})
        with pytest.raises(DataValidationError, match="缺少"):
            DataValidator.validate_dataframe(df, required_columns={"日期", "收盘"})

    def test_column_alias_english_passes(self):
        """英文列名应通过中文别名校验"""
        df = pd.DataFrame({"date": ["2026-01-01"], "close": [100]})
        assert DataValidator.validate_dataframe(df, required_columns={"日期", "收盘"}) is True

    def test_no_required_columns_skips_check(self):
        df = pd.DataFrame({"任意列": [1]})
        assert DataValidator.validate_dataframe(df) is True


class TestValidateHistData:
    """validate_hist_data: 历史K线校验"""

    def test_valid_hist(self):
        df = pd.DataFrame({
            "日期": ["2026-01-01"], "开盘": [100], "收盘": [105],
            "最高": [110], "最低": [95], "成交量": [1000],
        })
        assert DataValidator.validate_hist_data(df) is True

    def test_missing_column(self):
        df = pd.DataFrame({"日期": ["2026-01-01"]})
        with pytest.raises(DataValidationError):
            DataValidator.validate_hist_data(df)


class TestValidateSpotData:
    """validate_spot_data: 全市场行情校验"""

    def test_insufficient_rows(self):
        """spot 要求至少 10 行"""
        df = pd.DataFrame({"代码": ["000001"], "最新价": [10]})
        with pytest.raises(DataValidationError):
            DataValidator.validate_spot_data(df)

    def test_valid_spot(self):
        df = pd.DataFrame({"代码": [f"{i:06d}" for i in range(20)], "最新价": [10] * 20})
        assert DataValidator.validate_spot_data(df) is True


class TestCheckPriceAnomaly:
    """check_price_anomaly: 价格异常检测"""

    def test_normal_prices(self):
        df = pd.DataFrame({"收盘": [100, 200, 300]})
        assert DataValidator.check_price_anomaly(df) is True

    def test_negative_price(self):
        df = pd.DataFrame({"收盘": [100, -50]})
        assert DataValidator.check_price_anomaly(df) is False

    def test_extreme_price(self):
        df = pd.DataFrame({"收盘": [100, 2000000]})
        assert DataValidator.check_price_anomaly(df) is False

    def test_no_price_columns(self):
        df = pd.DataFrame({"其他列": [1, 2]})
        assert DataValidator.check_price_anomaly(df) is True


class TestCheckVolumeAnomaly:
    """check_volume_anomaly: 成交量异常检测"""

    def test_normal(self):
        df = pd.DataFrame({"成交量": [100, 200]})
        assert DataValidator.check_volume_anomaly(df) is True

    def test_negative(self):
        df = pd.DataFrame({"成交量": [100, -50]})
        assert DataValidator.check_volume_anomaly(df) is False

    def test_no_column(self):
        df = pd.DataFrame({"其他列": [1]})
        assert DataValidator.check_volume_anomaly(df) is True


class TestValidateAndRaise:
    """validate_and_raise: 类型分支"""

    def test_hist_type(self):
        df = pd.DataFrame({
            "日期": ["2026-01-01"], "开盘": [100], "收盘": [105],
            "最高": [110], "最低": [95], "成交量": [1000],
        })
        assert validate_and_raise(df, "hist") is True

    def test_realtime_type(self):
        df = pd.DataFrame({"代码": ["000001"], "最新价": [10]})
        assert validate_and_raise(df, "realtime") is True

    def test_unknown_type(self):
        df = pd.DataFrame({"任意列": [1]})
        assert validate_and_raise(df, "unknown") is True


class TestSafeValidate:
    """safe_validate: 异常捕获"""

    def test_valid_returns_true(self):
        df = pd.DataFrame({
            "日期": ["2026-01-01"], "开盘": [100], "收盘": [105],
            "最高": [110], "最低": [95], "成交量": [1000],
        })
        assert safe_validate(df, "hist") is True

    def test_invalid_returns_false(self):
        assert safe_validate(None, "hist") is False
