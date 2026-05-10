import unittest
import json
import logging
import importlib.util
from pathlib import Path

_SKILL_MAIN_PATH = Path(__file__).resolve().parent.parent.parent / 'tools' / 'skills' / 'valuation' / 'main.py'

TEST_SYMBOLS = ['600519', '688498', '688256', '301308']


def _load_skill():
    spec = importlib.util.spec_from_file_location('valuation_main', _SKILL_MAIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    logger = logging.getLogger('test_valuation')
    logger.setLevel(logging.WARNING)
    return mod.ValuationSkill(logger, None)


def _parse_result(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return {'raw': result}
    return result


class TestValuationSkillRealData(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.skill = _load_skill()

    def test_get_valuation_indicators(self):
        for symbol in TEST_SYMBOLS:
            with self.subTest(symbol=symbol):
                result = _parse_result(self.skill.get_valuation_indicators(symbol))
                self.assertNotIn('error', result, f'{symbol} 估值指标获取失败: {result.get("error")}')
                self.assertEqual(result.get('code'), symbol)
                has_indicator = any(k for k in result if k not in ('code',) and '_level' not in k)
                self.assertTrue(has_indicator, f'{symbol} 未获取到任何估值指标')
                print(f'\n  [{symbol}] 估值指标: {json.dumps(result, ensure_ascii=False)}')

    def test_get_valuation_indicators_cached(self):
        result = _parse_result(self.skill.get_valuation_indicators('600519'))
        self.assertNotIn('error', result)
        self.assertIn('code', result)
        print(f'\n  [600519] 缓存读取: {json.dumps(result, ensure_ascii=False)}')

    def test_get_valuation_percentile(self):
        result = _parse_result(self.skill.get_valuation_percentile('600519', years=5))
        self.assertNotIn('error', result, f'历史分位获取失败: {result.get("error")}')
        print(f'\n  [600519] 历史分位: {json.dumps(result, ensure_ascii=False, default=str)}')

    def test_get_industry_valuation_compare(self):
        result = _parse_result(self.skill.get_industry_valuation_compare('600519'))
        print(f'\n  [600519] 行业对比: {json.dumps(result, ensure_ascii=False, default=str)}')

    def test_get_valuation_summary(self):
        result = _parse_result(self.skill.get_valuation_summary('600519'))
        self.assertIn('valuation_indicators', result)
        self.assertIn('industry_compare', result)
        self.assertIn('percentile', result)
        print(f'\n  [600519] 估值综合: {json.dumps(result, ensure_ascii=False, default=str, indent=2)}')

    def test_calc_ddm_valuation(self):
        result = _parse_result(self.skill.calc_ddm_valuation('600519'))
        self.assertNotIn('error', result, f'DDM估值失败: {result.get("error")}')
        self.assertIn('intrinsic_value', result)
        self.assertIn('safety_margin', result)
        print(f'\n  [600519] DDM估值: {json.dumps(result, ensure_ascii=False, default=str)}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
