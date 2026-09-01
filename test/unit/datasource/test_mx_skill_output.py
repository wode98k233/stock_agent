"""
mx skill 输出结构测试。

只 monkeypatch 本地 MXData 类，不调用真实东方财富 API。
"""
import json


def test_mx_data_query_core_returns_structured_tables_without_api(monkeypatch):
    """mx_data_query 应返回结构化 tables 预览，供模板插槽提取。"""
    import tools.other_skills.eastmoney.mx_data as mx_data_module
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    class FakeMXData:
        def query(self, query):
            return {"status": 0}

        def parse_result(self, result):
            return (
                [{
                    "sheet_name": "行情",
                    "fieldnames": ["名称", "RSI", "涨跌幅"],
                    "rows": [{"名称": "测试股", "RSI": "65.3", "涨跌幅": "5.2"}],
                }],
                [],
                1,
                None,
            )

        def format_terminal(self, result, tables, total_rows):
            return "terminal preview"

    monkeypatch.setattr(mx_data_module, "MXData", FakeMXData)

    result = json.loads(_mx_data_query_core("测试查询"))

    assert result["terminal_output"] == "terminal preview"
    assert result["tables"][0]["rows"][0]["RSI"] == "65.3"
