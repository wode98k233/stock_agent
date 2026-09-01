import unittest
import pandas as pd
import numpy as np
from tools.valuation import (
    interpret_valuation_indicators,
    calc_industry_compare,
    calc_percentile,
    calc_dcf,
    calc_ddm,
    _classify,
    _relative_judge,
    _safety_margin_level,
    PE_THRESHOLDS,
    PB_THRESHOLDS,
    PS_THRESHOLDS,
    PEG_THRESHOLDS,
    DIVIDEND_YIELD_THRESHOLDS,
    PERCENTILE_LEVELS,
)


class TestClassify(unittest.TestCase):

    def test_pe_thresholds(self):
        cases = [(5, '极度低估'), (15, '低估'), (30, '合理'), (55, '偏高'), (80, '高估')]
        for value, expected in cases:
            with self.subTest(value=value):
                result = _classify(value, PE_THRESHOLDS)
                self.assertEqual(result, expected)
                print(f'  PE={value} → {result}')

    def test_pb_thresholds(self):
        cases = [(0.3, '极度低估'), (0.8, '低估'), (2.0, '合理'), (4.5, '偏高'), (7.0, '高估')]
        for value, expected in cases:
            with self.subTest(value=value):
                result = _classify(value, PB_THRESHOLDS)
                self.assertEqual(result, expected)
                print(f'  PB={value} → {result}')

    def test_negative_value(self):
        result = _classify(-5, [(0, 10, '低估'), (10, 30, '合理')])
        self.assertEqual(result, 'N/A')
        print(f'  负值-5 → {result}')


class TestInterpretValuationIndicators(unittest.TestCase):

    def test_basic_valuation(self):
        data = {
            'code': '600519',
            '市盈率-动态': 25.5,
            '市净率': 3.2,
            '市销率': 8.5,
        }
        result = interpret_valuation_indicators(data)
        self.assertEqual(result['code'], '600519')
        self.assertIn('pe_ttm', result)
        self.assertIn('pb', result)
        print(f'  600519 估值解读: PE={result.get("pe_ttm")}({result.get("pe_ttm_level")}), PB={result.get("pb")}({result.get("pb_level")}), PS={result.get("ps_ttm")}({result.get("ps_ttm_level")})')

    def test_mx_data_fields(self):
        data = {
            'code': '600519',
            '市盈率(TTM)': 20.82,
            '市净率': 6.36,
        }
        result = interpret_valuation_indicators(data)
        self.assertIn('pe_ttm', result)
        self.assertEqual(result['pe_ttm'], 20.82)
        self.assertEqual(result['pb'], 6.36)
        print(f'  妙想字段映射: PE(TTM)={result["pe_ttm"]}({result["pe_ttm_level"]}), PB={result["pb"]}({result["pb_level"]})')

    def test_missing_fields(self):
        data = {'code': '000001'}
        result = interpret_valuation_indicators(data)
        self.assertEqual(result['code'], '000001')
        self.assertNotIn('pe_ttm', result)
        self.assertNotIn('pb', result)
        print(f'  缺失字段: 仅返回code={result["code"]}')

    def test_zero_pe(self):
        data = {'code': '600519', '市盈率-动态': 0}
        result = interpret_valuation_indicators(data)
        self.assertNotIn('pe_ttm', result)
        print(f'  PE=0: 正确忽略')

    def test_peg_valuation(self):
        data = {'code': '600519', 'PEG': 0.8}
        result = interpret_valuation_indicators(data)
        self.assertIn('peg', result)
        self.assertEqual(result['peg_level'], '低估')
        print(f'  PEG=0.8 → {result["peg_level"]}')

    def test_dividend_yield(self):
        data = {'code': '600519', '股息率': 4.5}
        result = interpret_valuation_indicators(data)
        self.assertIn('dividend_yield', result)
        self.assertEqual(result['dividend_yield_level'], '较高')
        print(f'  股息率=4.5% → {result["dividend_yield_level"]}')


class TestCalcIndustryCompare(unittest.TestCase):

    def test_stock_lower_than_industry(self):
        stock_val = {'pe_ttm': 30.0, 'pb': 2.5}
        industry_data = {'行业': '白酒', '股票数量': 20, 'PE均值': 35.0, 'PE中位数': 32.0, 'PB均值': 3.0, 'PB中位数': 2.8}
        result = calc_industry_compare(stock_val, industry_data)
        self.assertEqual(result['industry'], '白酒')
        self.assertLess(result['pe_deviation_pct'], 0)
        self.assertLess(result['pb_deviation_pct'], 0)
        print(f'  白酒行业对比: PE偏离={result["pe_deviation_pct"]}%({result["pe_relative"]}), PB偏离={result["pb_deviation_pct"]}%({result["pb_relative"]})')

    def test_stock_higher_than_industry(self):
        stock_val = {'pe_ttm': 50.0, 'pb': 5.0}
        industry_data = {'行业': '科技', '股票数量': 30, 'PE均值': 30.0, 'PB均值': 3.0}
        result = calc_industry_compare(stock_val, industry_data)
        self.assertGreater(result['pe_deviation_pct'], 0)
        self.assertEqual(result['pe_relative'], '远高于行业')
        print(f'  科技行业对比: PE偏离={result["pe_deviation_pct"]}%({result["pe_relative"]}), PB偏离={result["pb_deviation_pct"]}%({result["pb_relative"]})')

    def test_missing_industry_data(self):
        stock_val = {'pe_ttm': 30.0, 'pb': 2.5}
        industry_data = {'行业': '未知'}
        result = calc_industry_compare(stock_val, industry_data)
        self.assertNotIn('pe_deviation_pct', result)
        print(f'  缺失行业数据: 无偏离度计算')


class TestCalcPercentile(unittest.TestCase):

    def test_basic_percentile(self):
        np.random.seed(42)
        pe_values = np.random.uniform(10, 50, 500)
        pb_values = np.random.uniform(0.5, 5, 500)
        history_df = pd.DataFrame({'pe_ttm': pe_values, 'pb': pb_values})
        current_val = {'pe_ttm': 30.0, 'pb': 2.5}
        result = calc_percentile(history_df, current_val, years=5)
        self.assertIn('pe_percentile', result)
        self.assertIn('pb_percentile', result)
        print(f'  PE分位={result["pe_percentile"]}%({result["pe_level"]}), PB分位={result["pb_percentile"]}%({result["pb_level"]})')

    def test_extremely_low_percentile(self):
        pe_values = list(range(20, 60))
        history_df = pd.DataFrame({'pe_ttm': pe_values})
        current_val = {'pe_ttm': 21.0}
        result = calc_percentile(history_df, current_val)
        self.assertLess(result['pe_percentile'], 10)
        self.assertEqual(result['pe_level'], '极度低估')
        print(f'  PE=21(历史20-59) → 分位={result["pe_percentile"]}%({result["pe_level"]})')

    def test_extremely_high_percentile(self):
        pe_values = list(range(20, 60))
        history_df = pd.DataFrame({'pe_ttm': pe_values})
        current_val = {'pe_ttm': 58.0}
        result = calc_percentile(history_df, current_val)
        self.assertGreater(result['pe_percentile'], 90)
        self.assertEqual(result['pe_level'], '极度高估')
        print(f'  PE=58(历史20-59) → 分位={result["pe_percentile"]}%({result["pe_level"]})')

    def test_empty_history(self):
        history_df = pd.DataFrame()
        current_val = {'pe_ttm': 30.0}
        result = calc_percentile(history_df, current_val)
        self.assertNotIn('pe_percentile', result)
        print(f'  空历史数据: 无分位计算')


class TestCalcDCF(unittest.TestCase):

    def test_basic_dcf(self):
        financial = {
            'net_profit': 5000000000,
            'depreciation': 1000000000,
            'capex': 2000000000,
            'total_shares': 1256197890,
        }
        result = calc_dcf('600519', financial, current_price=1800.0)
        self.assertIn('intrinsic_value', result)
        self.assertIn('safety_margin', result)
        self.assertNotIn('error', result)
        print(f'  600519 DCF: 内在价值={result["intrinsic_value"]}, 当前价={result["current_price"]}, 安全边际={result["safety_margin"]}%({result["valuation_level"]})')

    def test_no_profit(self):
        financial = {'net_profit': -1000000000}
        result = calc_dcf('000001', financial, current_price=10.0)
        self.assertIn('error', result)
        print(f'  亏损企业: {result["error"]}')

    def test_custom_params(self):
        financial = {
            'net_profit': 5000000000,
            'depreciation': 1000000000,
            'capex': 2000000000,
            'total_shares': 1256197890,
        }
        result = calc_dcf('600519', financial, current_price=1800.0,
                          growth_rate=0.12, wacc=0.08, terminal_growth=0.02)
        self.assertEqual(result['assumptions']['growth_rate'], 0.12)
        self.assertEqual(result['assumptions']['wacc'], 0.08)
        print(f'  自定义参数: 增长率=12%, WACC=8%, 永续=2% → 内在价值={result["intrinsic_value"]}, 安全边际={result["safety_margin"]}%')


class TestCalcDDM(unittest.TestCase):

    def test_basic_ddm(self):
        financial = {'dividend_per_share': 21.91}
        result = calc_ddm('600519', financial, current_price=1800.0)
        self.assertIn('intrinsic_value', result)
        self.assertIn('safety_margin', result)
        self.assertNotIn('error', result)
        print(f'  600519 DDM: 内在价值={result["intrinsic_value"]}, 当前价={result["current_price"]}, 安全边际={result["safety_margin"]}%({result["valuation_level"]})')

    def test_no_dividend(self):
        financial = {}
        result = calc_ddm('000001', financial, current_price=10.0)
        self.assertIn('error', result)
        print(f'  无股利: {result["error"]}')

    def test_invalid_params(self):
        financial = {'dividend_per_share': 5.0}
        result = calc_ddm('600519', financial, current_price=1800.0,
                          growth_rate=0.12, required_rate=0.10)
        self.assertIn('error', result)
        print(f'  增长率>要求回报率: {result["error"]}')


class TestRelativeJudge(unittest.TestCase):

    def test_all_levels(self):
        cases = [(-30, '远低于行业'), (-10, '低于行业'), (3, '接近行业'), (10, '高于行业'), (30, '远高于行业')]
        for deviation, expected in cases:
            with self.subTest(deviation=deviation):
                result = _relative_judge(deviation)
                self.assertEqual(result, expected)
                print(f'  偏离{deviation:+d}% → {result}')


class TestSafetyMarginLevel(unittest.TestCase):

    def test_all_levels(self):
        cases = [(50, '极度低估'), (20, '低估'), (0, '合理'), (-20, '偏高'), (-40, '高估')]
        for margin, expected in cases:
            with self.subTest(margin=margin):
                result = _safety_margin_level(margin)
                self.assertEqual(result, expected)
                print(f'  安全边际{margin:+d}% → {result}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
