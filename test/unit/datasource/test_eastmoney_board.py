"""Test: 东财 push2 板块成分股直连（按 BK 代码）。网络 mock，不依赖外部。"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import unittest
from unittest.mock import patch, MagicMock


def _fake_resp(diff):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {'data': {'diff': diff}}
    return resp


class TestFetchBoardConsByBk(unittest.TestCase):

    def test_maps_diff_to_dataframe(self):
        from tools.fetcher.eastmoney_board import fetch_board_cons_by_bk
        diff = [
            {'f12': '600519', 'f14': '贵州茅台', 'f2': 1800.0, 'f3': 1.2},
            {'f12': '000858', 'f14': '五粮液', 'f2': 150.0, 'f3': -0.5},
        ]
        with patch('tools.fetcher.eastmoney_board.requests.get', return_value=_fake_resp(diff)) as g:
            df = fetch_board_cons_by_bk('BK0477')
        self.assertEqual(list(df.columns), ['代码', '名称', '最新价', '涨跌幅'])
        self.assertEqual(df.iloc[0]['代码'], '600519')
        self.assertEqual(df.iloc[0]['名称'], '贵州茅台')
        self.assertEqual(len(df), 2)
        # 确认 fs 参数按 BK 代码拼装
        self.assertEqual(g.call_args.kwargs['params']['fs'], 'b:BK0477')

    def test_pads_short_code(self):
        from tools.fetcher.eastmoney_board import fetch_board_cons_by_bk
        diff = [{'f12': '1', 'f14': '平安银行', 'f2': 12.0, 'f3': 0.1}]
        with patch('tools.fetcher.eastmoney_board.requests.get', return_value=_fake_resp(diff)):
            df = fetch_board_cons_by_bk('BK0001')
        self.assertEqual(df.iloc[0]['代码'], '000001')

    def test_empty_diff_returns_empty(self):
        from tools.fetcher.eastmoney_board import fetch_board_cons_by_bk
        with patch('tools.fetcher.eastmoney_board.requests.get', return_value=_fake_resp([])):
            df = fetch_board_cons_by_bk('BK9999')
        self.assertTrue(df.empty)

    def test_blank_bk_returns_empty_no_request(self):
        from tools.fetcher.eastmoney_board import fetch_board_cons_by_bk
        with patch('tools.fetcher.eastmoney_board.requests.get') as g:
            df = fetch_board_cons_by_bk('')
        self.assertTrue(df.empty)
        g.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
