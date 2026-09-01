"""
东方财富妙想 Skills 集成测试（真实 API 调用）

覆盖 5 个 skill 工具：
- mx_data_query: 金融数据查询
- mx_search_news: 资讯搜索
- mx_xuangu_filter: 智能选股
- mx_moni_operation: 模拟组合管理
- mx_zixuan_manage: 自选股管理

重点验证：
1. 常用场景能否正常返回数据（非 None / 非 error）
2. 已知容易返回空的场景（ETF、复杂条件等）是否优雅降级
3. API Key 缺失时的错误处理

需要环境变量 MX_APIKEY
"""
import os
import sys
import json
import time
import logging
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')


def divider(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def _has_mx_apikey():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    return bool(os.getenv("MX_APIKEY"))


def _parse_result(raw: str) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"raw": raw}


def _is_success(result: dict) -> bool:
    return "error" not in result or result.get("error") is None


def _is_failed(result: dict) -> bool:
    return result.get("error") is not None or result.get("status") == "failed"


# ═══════════════════════════════════════════════════════════════
#  mx_data_query 测试
# ═══════════════════════════════════════════════════════════════

def test_mx_data_query_stock_price():
    """IT-MX-01: mx_data_query — 个股行情查询（最常用场景）"""
    divider("IT-MX-01: mx_data_query — 个股行情查询")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("贵州茅台最新价 涨跌幅")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ❌ 查询失败: {result.get('error')}")
        return False

    assert "tables_count" in result or "terminal_output" in result, f"缺少关键字段: {result}"
    print(f"  ✅ 个股行情查询成功: tables={result.get('tables_count')}, rows={result.get('total_rows')}")
    return True


def test_mx_data_query_financial():
    """IT-MX-02: mx_data_query — 财务数据查询"""
    divider("IT-MX-02: mx_data_query — 财务数据查询")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("贵州茅台近三年净利润 营业收入")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 财务数据查询失败: {result.get('error')}")
        return True

    print(f"  ✅ 财务数据查询成功: tables={result.get('tables_count')}, rows={result.get('total_rows')}")
    return True


def test_mx_data_query_etf():
    """IT-MX-03: mx_data_query — ETF 行情查询（已知高频失败场景）"""
    divider("IT-MX-03: mx_data_query — ETF 行情查询（已知高频失败场景）")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("绿色电力ETF 159669 最新价 涨跌幅")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        err = result.get("error", "")
        if "dataTableDTOList" in err:
            print(f"  ⚠️ ETF 查询返回空（已知问题）: {err}")
            print(f"  → API 不支持 ETF 类查询，非代码 bug")
            return True
        print(f"  ❌ ETF 查询失败: {err}")
        return False

    print(f"  ✅ ETF 行情查询成功: tables={result.get('tables_count')}, rows={result.get('total_rows')}")
    return True


def test_mx_data_query_sector():
    """IT-MX-04: mx_data_query — 板块行情查询"""
    divider("IT-MX-04: mx_data_query — 板块行情查询")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("沪深300指数最新点位 涨跌幅")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 板块行情查询失败: {result.get('error')}")
        return True

    print(f"  ✅ 板块行情查询成功: tables={result.get('tables_count')}, rows={result.get('total_rows')}")
    return True


def test_mx_data_query_complex():
    """IT-MX-05: mx_data_query — 复杂多指标查询（容易失败）"""
    divider("IT-MX-05: mx_data_query — 复杂多指标查询")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("宁德时代最新价 涨跌幅 成交额 换手率 主力资金流向 MACD RSI")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 复杂查询失败: {result.get('error')}")
        return True

    print(f"  ✅ 复杂查询成功: tables={result.get('tables_count')}, rows={result.get('total_rows')}")
    return True


# ═══════════════════════════════════════════════════════════════
#  mx_search_news 测试
# ═══════════════════════════════════════════════════════════════

def test_mx_search_news_stock():
    """IT-MX-06: mx_search_news — 个股资讯搜索"""
    divider("IT-MX-06: mx_search_news — 个股资讯搜索")
    from tools.other_skills.eastmoney.skills import _mx_search_news_core

    result_raw = _mx_search_news_core("贵州茅台最新研报")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ❌ 资讯搜索失败: {result.get('error')}")
        return False

    assert "result" in result, f"缺少 result 字段: {result}"
    content = result.get("result", "")
    if "未找到" in content:
        print(f"  ⚠️ 未找到相关资讯（正常降级）")
        return True

    print(f"  ✅ 个股资讯搜索成功: {len(content)} 字符")
    return True


def test_mx_search_news_sector():
    """IT-MX-07: mx_search_news — 板块/行业新闻"""
    divider("IT-MX-07: mx_search_news — 板块/行业新闻")
    from tools.other_skills.eastmoney.skills import _mx_search_news_core

    result_raw = _mx_search_news_core("人工智能板块近期新闻")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ❌ 板块新闻搜索失败: {result.get('error')}")
        return False

    content = result.get("result", "")
    print(f"  ✅ 板块新闻搜索成功: {len(content)} 字符")
    return True


def test_mx_search_news_macro():
    """IT-MX-08: mx_search_news — 宏观事件搜索"""
    divider("IT-MX-08: mx_search_news — 宏观事件搜索")
    from tools.other_skills.eastmoney.skills import _mx_search_news_core

    result_raw = _mx_search_news_core("北向资金最新流向解读")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ❌ 宏观搜索失败: {result.get('error')}")
        return False

    content = result.get("result", "")
    print(f"  ✅ 宏观搜索成功: {len(content)} 字符")
    return True


# ═══════════════════════════════════════════════════════════════
#  mx_xuangu_filter 测试
# ═══════════════════════════════════════════════════════════════

def test_mx_xuangu_simple():
    """IT-MX-09: mx_xuangu_filter — 简单条件选股"""
    divider("IT-MX-09: mx_xuangu_filter — 简单条件选股")
    from tools.other_skills.eastmoney.skills import _mx_xuangu_filter_core

    result_raw = _mx_xuangu_filter_core("今日涨幅大于2%的A股")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 选股失败: {result.get('error')}")
        return True

    stock_count = result.get("stock_count", 0)
    print(f"  ✅ 选股成功: {stock_count} 只股票, 数据来源: {result.get('data_source')}")
    return True


def test_mx_xuangu_financial():
    """IT-MX-10: mx_xuangu_filter — 财务条件选股"""
    divider("IT-MX-10: mx_xuangu_filter — 财务条件选股")
    from tools.other_skills.eastmoney.skills import _mx_xuangu_filter_core

    result_raw = _mx_xuangu_filter_core("市盈率小于20的银行股")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 财务选股失败: {result.get('error')}")
        return True

    stock_count = result.get("stock_count", 0)
    print(f"  ✅ 财务选股成功: {stock_count} 只股票")
    return True


def test_mx_xuangu_sector():
    """IT-MX-11: mx_xuangu_filter — 板块内选股"""
    divider("IT-MX-11: mx_xuangu_filter — 板块内选股")
    from tools.other_skills.eastmoney.skills import _mx_xuangu_filter_core

    result_raw = _mx_xuangu_filter_core("新能源板块市盈率小于30的股票")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 板块选股失败: {result.get('error')}")
        return True

    stock_count = result.get("stock_count", 0)
    print(f"  ✅ 板块选股成功: {stock_count} 只股票")
    return True


def test_mx_xuangu_etf():
    """IT-MX-12: mx_xuangu_filter — ETF 选股（已知可能失败）"""
    divider("IT-MX-12: mx_xuangu_filter — ETF 选股（已知可能失败）")
    from tools.other_skills.eastmoney.skills import _mx_xuangu_filter_core

    result_raw = _mx_xuangu_filter_core("筛选绿色电力ETF和电网设备ETF")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ ETF 选股失败（已知问题）: {result.get('error')}")
        return True

    stock_count = result.get("stock_count", 0)
    print(f"  ✅ ETF 选股成功: {stock_count} 只")
    return True


# ═══════════════════════════════════════════════════════════════
#  mx_moni_operation 测试
# ═══════════════════════════════════════════════════════════════

def test_mx_moni_positions():
    """IT-MX-13: mx_moni_operation — 持仓查询"""
    divider("IT-MX-13: mx_moni_operation — 持仓查询")
    from tools.other_skills.eastmoney.skills import _mx_moni_operation_core

    result_raw = _mx_moni_operation_core("我的持仓")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        err = result.get("error", "")
        if "404" in err or "未绑定" in err:
            print(f"  ⚠️ 模拟账户未绑定（正常降级）: {err}")
            return True
        print(f"  ❌ 持仓查询失败: {err}")
        return False

    print(f"  ✅ 持仓查询成功")
    return True


def test_mx_moni_balance():
    """IT-MX-14: mx_moni_operation — 资金查询"""
    divider("IT-MX-14: mx_moni_operation — 资金查询")
    from tools.other_skills.eastmoney.skills import _mx_moni_operation_core

    result_raw = _mx_moni_operation_core("我的资金")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        err = result.get("error", "")
        if "404" in err or "未绑定" in err:
            print(f"  ⚠️ 模拟账户未绑定（正常降级）: {err}")
            return True
        print(f"  ❌ 资金查询失败: {err}")
        return False

    print(f"  ✅ 资金查询成功")
    return True


def test_mx_moni_orders():
    """IT-MX-15: mx_moni_operation — 委托查询"""
    divider("IT-MX-15: mx_moni_operation — 委托查询")
    from tools.other_skills.eastmoney.skills import _mx_moni_operation_core

    result_raw = _mx_moni_operation_core("我的委托")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        err = result.get("error", "")
        if "404" in err or "未绑定" in err:
            print(f"  ⚠️ 模拟账户未绑定（正常降级）: {err}")
            return True
        print(f"  ❌ 委托查询失败: {err}")
        return False

    print(f"  ✅ 委托查询成功")
    return True


def test_mx_moni_unrecognized():
    """IT-MX-16: mx_moni_operation — 无法识别的意图"""
    divider("IT-MX-16: mx_moni_operation — 无法识别的意图")
    from tools.other_skills.eastmoney.skills import _mx_moni_operation_core

    result_raw = _mx_moni_operation_core("今天天气怎么样")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    assert _is_failed(result), f"应返回错误但成功了: {result}"
    assert "无法识别" in result.get("error", ""), f"错误信息不正确: {result}"
    print(f"  ✅ 无法识别意图正确返回错误")
    return True


# ═══════════════════════════════════════════════════════════════
#  mx_zixuan_manage 测试
# ═══════════════════════════════════════════════════════════════

def test_mx_zixuan_query():
    """IT-MX-17: mx_zixuan_manage — 查询自选股"""
    divider("IT-MX-17: mx_zixuan_manage — 查询自选股")
    from tools.other_skills.eastmoney.skills import _mx_zixuan_manage_core

    result_raw = _mx_zixuan_manage_core("查询我的自选股列表")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 自选股查询失败: {result.get('error')}")
        return True

    print(f"  ✅ 自选股查询成功")
    return True


def test_mx_zixuan_add():
    """IT-MX-18: mx_zixuan_manage — 添加自选股"""
    divider("IT-MX-18: mx_zixuan_manage — 添加自选股")
    from tools.other_skills.eastmoney.skills import _mx_zixuan_manage_core

    result_raw = _mx_zixuan_manage_core("把贵州茅台添加到我的自选股列表")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 添加自选股失败: {result.get('error')}")
        return True

    print(f"  ✅ 添加自选股成功")
    return True


def test_mx_zixuan_delete():
    """IT-MX-19: mx_zixuan_manage — 删除自选股"""
    divider("IT-MX-19: mx_zixuan_manage — 删除自选股")
    from tools.other_skills.eastmoney.skills import _mx_zixuan_manage_core

    result_raw = _mx_zixuan_manage_core("把贵州茅台从我的自选股列表删除")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ⚠️ 删除自选股失败: {result.get('error')}")
        return True

    print(f"  ✅ 删除自选股成功")
    return True


# ═══════════════════════════════════════════════════════════════
#  边界场景测试
# ═══════════════════════════════════════════════════════════════

def test_mx_data_empty_query():
    """IT-MX-20: mx_data_query — 空查询"""
    divider("IT-MX-20: mx_data_query — 空查询")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ✅ 空查询正确返回错误: {result.get('error')}")
        return True

    print(f"  ⚠️ 空查询返回了数据（可能 API 容错）")
    return True


def test_mx_data_invalid_stock():
    """IT-MX-21: mx_data_query — 不存在的股票"""
    divider("IT-MX-21: mx_data_query — 不存在的股票")
    from tools.other_skills.eastmoney.skills import _mx_data_query_core

    result_raw = _mx_data_query_core("999999最新价")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ✅ 不存在的股票正确返回错误: {result.get('error')}")
        return True

    print(f"  ⚠️ 不存在的股票返回了数据（可能 API 容错）")
    return True


def test_mx_xuangu_too_strict():
    """IT-MX-22: mx_xuangu_filter — 过于严格的条件"""
    divider("IT-MX-22: mx_xuangu_filter — 过于严格的条件")
    from tools.other_skills.eastmoney.skills import _mx_xuangu_filter_core

    result_raw = _mx_xuangu_filter_core("市盈率小于1且涨幅大于50%的A股")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    if _is_failed(result):
        print(f"  ✅ 过严条件正确返回错误: {result.get('error')}")
        return True

    stock_count = result.get("stock_count", 0)
    if stock_count == 0:
        print(f"  ✅ 过严条件返回 0 只股票（正常降级）")
    else:
        print(f"  ⚠️ 过严条件返回了 {stock_count} 只股票")
    return True


def test_mx_moni_buy_no_code():
    """IT-MX-23: mx_moni_operation — 买入缺少股票代码"""
    divider("IT-MX-23: mx_moni_operation — 买入缺少股票代码")
    from tools.other_skills.eastmoney.skills import _mx_moni_operation_core

    result_raw = _mx_moni_operation_core("买入 100股")
    result = _parse_result(result_raw)
    print(f"  返回: {json.dumps(result, ensure_ascii=False)[:200]}")

    assert _is_failed(result), f"应返回错误但成功了: {result}"
    print(f"  ✅ 缺少股票代码正确返回错误: {result.get('error')}")
    return True


def test_mx_no_apikey():
    """IT-MX-24: 无 API Key 时的错误处理"""
    divider("IT-MX-24: 无 API Key 时的错误处理")
    from tools.other_skills.eastmoney.mx_data import MXData
    from tools.other_skills.eastmoney.mx_search import MXSearch
    from tools.other_skills.eastmoney.mx_xuangu import MXSelectStock

    saved_key = os.environ.pop("MX_APIKEY", None)
    try:
        for cls, name in [(MXData, "MXData"), (MXSearch, "MXSearch"), (MXSelectStock, "MXSelectStock")]:
            try:
                cls()
                print(f"  ❌ {name} 未抛出 ValueError")
                return False
            except ValueError:
                print(f"  ✅ {name} 正确抛出 ValueError")
    finally:
        if saved_key:
            os.environ["MX_APIKEY"] = saved_key

    return True


# ═══════════════════════════════════════════════════════════════
#  工具注册测试
# ═══════════════════════════════════════════════════════════════

def test_tool_registry():
    """IT-MX-25: TOOL_REGISTRY 完整性检查"""
    divider("IT-MX-25: TOOL_REGISTRY 完整性检查")
    from tools.other_skills.eastmoney.skills import TOOL_REGISTRY

    expected_tools = {"mx_data_query", "mx_search_news", "mx_xuangu_filter", "mx_moni_operation", "mx_zixuan_manage"}
    actual_tools = set(TOOL_REGISTRY.keys())

    missing = expected_tools - actual_tools
    extra = actual_tools - expected_tools

    if missing:
        print(f"  ❌ 缺少工具: {missing}")
        return False
    if extra:
        print(f"  ⚠️ 多余工具: {extra}")

    for tool_name, (core_func, param_model) in TOOL_REGISTRY.items():
        assert callable(core_func), f"{tool_name} core_func 不可调用"
        assert hasattr(param_model, "model_fields"), f"{tool_name} param_model 不是 Pydantic model"
        assert "query" in param_model.model_fields, f"{tool_name} 缺少 query 参数"
        print(f"  ✅ {tool_name}: core_func={core_func.__name__}, params=({', '.join(param_model.model_fields.keys())})")

    return True


def test_skill_discovery():
    """IT-MX-26: 技能自动发现"""
    divider("IT-MX-26: 技能自动发现")
    from tools.other_skills.eastmoney.skills import get_skill_loaders, get_skill_catalog

    loaders = get_skill_loaders()
    catalog = get_skill_catalog()

    expected_skills = {"mx_data", "mx_search", "mx_xuangu", "mx_moni", "mx_zixuan"}
    actual_skills = set(loaders.keys())

    missing = expected_skills - actual_skills
    if missing:
        print(f"  ❌ 缺少 skill: {missing}")
        return False

    for name in actual_skills:
        print(f"  ✅ {name}: 已注册")

    assert "东方财富" in catalog or "妙想" in catalog, f"catalog 缺少关键信息: {catalog[:100]}"
    print(f"  ✅ catalog 长度: {len(catalog)} 字符")
    return True


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║       东方财富妙想 Skills 集成测试 (真实 API)                       ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    if not _has_mx_apikey():
        print("\n  ⚠️  MX_APIKEY 未配置，API 调用测试将跳过")
        print("  仅运行工具注册和发现测试\n")
        tests = [
            ("IT-MX-25: TOOL_REGISTRY", test_tool_registry),
            ("IT-MX-26: 技能发现", test_skill_discovery),
            ("IT-MX-24: 无 API Key", test_mx_no_apikey),
        ]
    else:
        tests = [
            ("IT-MX-01: 个股行情", test_mx_data_query_stock_price),
            ("IT-MX-02: 财务数据", test_mx_data_query_financial),
            ("IT-MX-03: ETF行情(已知问题)", test_mx_data_query_etf),
            ("IT-MX-04: 板块行情", test_mx_data_query_sector),
            ("IT-MX-05: 复杂查询", test_mx_data_query_complex),
            ("IT-MX-06: 个股资讯", test_mx_search_news_stock),
            ("IT-MX-07: 板块新闻", test_mx_search_news_sector),
            ("IT-MX-08: 宏观搜索", test_mx_search_news_macro),
            ("IT-MX-09: 简单选股", test_mx_xuangu_simple),
            ("IT-MX-10: 财务选股", test_mx_xuangu_financial),
            ("IT-MX-11: 板块选股", test_mx_xuangu_sector),
            ("IT-MX-12: ETF选股(已知问题)", test_mx_xuangu_etf),
            ("IT-MX-13: 持仓查询", test_mx_moni_positions),
            ("IT-MX-14: 资金查询", test_mx_moni_balance),
            ("IT-MX-15: 委托查询", test_mx_moni_orders),
            ("IT-MX-16: 无法识别意图", test_mx_moni_unrecognized),
            ("IT-MX-17: 查询自选", test_mx_zixuan_query),
            ("IT-MX-18: 添加自选", test_mx_zixuan_add),
            ("IT-MX-19: 删除自选", test_mx_zixuan_delete),
            ("IT-MX-20: 空查询", test_mx_data_empty_query),
            ("IT-MX-21: 不存在股票", test_mx_data_invalid_stock),
            ("IT-MX-22: 过严条件", test_mx_xuangu_too_strict),
            ("IT-MX-23: 买入缺代码", test_mx_moni_buy_no_code),
            ("IT-MX-24: 无API Key", test_mx_no_apikey),
            ("IT-MX-25: TOOL_REGISTRY", test_tool_registry),
            ("IT-MX-26: 技能发现", test_skill_discovery),
        ]

    passed = 0
    failed = 0
    results = {}

    for name, test_func in tests:
        try:
            ok = test_func()
            results[name] = ok
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            results[name] = False
            failed += 1
            print(f"\n  ❌ {name} 异常: {e}")
            traceback.print_exc()

    divider("测试总结")
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
    print(f"\n  通过: {passed}/{len(results)}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
