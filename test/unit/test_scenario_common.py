"""
Test: 场景处理器公共工具模块
验证: agents/scenarios/common.py
"""
import sys, os, asyncio, importlib.util

# 项目根目录: test/unit/ → test/ → project_root
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _project_root)

# 直接加载模块，避免 agents/__init__.py 的重依赖链
_common_path = os.path.join(_project_root, "agents", "scenarios", "common.py")
_spec = importlib.util.spec_from_file_location("agents.scenarios.common", _common_path)
_mod = importlib.util.module_from_spec(_spec)
# 预注册父包，让 common.py 内的 from agents.scenario_router import ... 能正常工作
sys.modules.setdefault("agents", type(sys)("agents"))
sys.modules["agents"].__path__ = [os.path.join(_project_root, "agents")]
_spec.loader.exec_module(_mod)

# 从加载的模块中导出所有待测函数
validate_stock_code = _mod.validate_stock_code
resolve_stock = _mod.resolve_stock
resolve_stocks = _mod.resolve_stocks
resolve_sector = _mod.resolve_sector
format_amount = _mod.format_amount
format_pct = _mod.format_pct
format_market_cap = _mod.format_market_cap


def test_validate_stock_code():
    print("=" * 60)
    print("TC-Common-01: validate_stock_code 合法代码")
    print("=" * 60)

    # 合法代码
    for code in ["600519", "000001", "300750", "688981"]:
        assert validate_stock_code(code) is True, f"{code} 应该合法"
        print(f"  {code} → True [OK]")

    # 非法代码
    for code in ["123456", "202505", "12345", "1234567", "", "abc"]:
        assert validate_stock_code(code) is False, f"{code} 应该非法"
        print(f"  {code!r} → False [OK]")

    print("[OK] validate_stock_code 正确\n")


def test_resolve_stock_from_names():
    print("=" * 60)
    print("TC-Common-02: resolve_stock 从 stock_names 解析")
    print("=" * 60)

    context = {
        "stock_codes": [],
        "stock_names": [{"name": "贵州茅台", "code": "600519"}],
    }
    code, name = resolve_stock(context)
    assert code == "600519", f"期望 600519，实际 {code}"
    assert name == "贵州茅台", f"期望 贵州茅台，实际 {name}"
    print(f"  结果: ({code}, {name}) [OK]")
    print("[OK] resolve_stock from names 正确\n")


def test_resolve_stock_from_codes():
    print("=" * 60)
    print("TC-Common-03: resolve_stock 从 stock_codes 解析（无名称时code当name）")
    print("=" * 60)

    context = {
        "stock_codes": ["600519"],
        "stock_names": [],
    }
    code, name = resolve_stock(context)
    assert code == "600519", f"期望 600519，实际 {code}"
    assert name == "600519", f"期望 600519（无名称时code当name），实际 {name}"
    print(f"  结果: ({code}, {name}) [OK]")
    print("[OK] resolve_stock from codes 正确\n")


def test_resolve_stock_empty():
    print("=" * 60)
    print("TC-Common-04: resolve_stock 空上下文")
    print("=" * 60)

    context = {"stock_codes": [], "stock_names": []}
    code, name = resolve_stock(context)
    assert code is None, f"期望 None，实际 {code}"
    assert name is None, f"期望 None，实际 {name}"
    print(f"  结果: ({code}, {name}) [OK]")
    print("[OK] resolve_stock empty 正确\n")


def test_resolve_stocks_multiple():
    print("=" * 60)
    print("TC-Common-05: resolve_stocks 多只股票解析")
    print("=" * 60)

    context = {
        "stock_codes": ["600519", "000001"],
        "stock_names": [
            {"name": "贵州茅台", "code": "600519"},
            {"name": "平安银行", "code": "000001"},
        ],
    }
    result = resolve_stocks(context)
    assert len(result) == 2, f"期望 2 只，实际 {len(result)}"
    assert result[0]["code"] == "600519"
    assert result[0]["name"] == "贵州茅台"
    assert result[1]["code"] == "000001"
    assert result[1]["name"] == "平安银行"
    print(f"  结果: {result} [OK]")
    print("[OK] resolve_stocks multiple 正确\n")


def test_resolve_stocks_max_count():
    print("=" * 60)
    print("TC-Common-06: resolve_stocks 超过限制截断")
    print("=" * 60)

    context = {
        "stock_codes": ["600519", "000001", "300750", "688981", "002594", "601318"],
        "stock_names": [],
    }
    result = resolve_stocks(context, max_count=3)
    assert len(result) == 3, f"期望 3 只，实际 {len(result)}"
    print(f"  结果: {len(result)} 只 [OK]")
    print("[OK] resolve_stocks max_count 正确\n")


def test_resolve_sector():
    print("=" * 60)
    print("TC-Common-07: resolve_sector 板块解析")
    print("=" * 60)

    # 从 context 解析
    context = {"sector_names": ["新能源"]}
    result = resolve_sector(context)
    assert result == "新能源", f"期望 新能源，实际 {result}"
    print(f"  从 context: {result} [OK]")

    # 从 user_input 兜底解析
    context2 = {"sector_names": []}
    result2 = resolve_sector(context2, user_input="分析一下半导体板块")
    assert result2 == "半导体", f"期望 半导体，实际 {result2}"
    print(f"  从 user_input: {result2} [OK]")
    print("[OK] resolve_sector 正确\n")


def test_resolve_sector_empty():
    print("=" * 60)
    print("TC-Common-08: resolve_sector 无板块返回 None")
    print("=" * 60)

    context = {"sector_names": []}
    result = resolve_sector(context, user_input="今天天气不错")
    assert result is None, f"期望 None，实际 {result}"
    print(f"  结果: {result} [OK]")
    print("[OK] resolve_sector empty 正确\n")


def test_format_amount():
    print("=" * 60)
    print("TC-Common-09: format_amount 金额格式化")
    print("=" * 60)

    assert format_amount(123456789) == "1.23亿", f"实际: {format_amount(123456789)}"
    print(f"  123456789 → {format_amount(123456789)} [OK]")

    assert format_amount(5000) == "5000", f"实际: {format_amount(5000)}"
    print(f"  5000 → {format_amount(5000)} [OK]")

    print("[OK] format_amount 正确\n")


def test_format_pct():
    print("=" * 60)
    print("TC-Common-10: format_pct 涨跌幅格式化")
    print("=" * 60)

    assert format_pct(3.56) == "+3.56%", f"实际: {format_pct(3.56)}"
    print(f"  3.56 → {format_pct(3.56)} [OK]")

    assert format_pct(-2.11) == "-2.11%", f"实际: {format_pct(-2.11)}"
    print(f"  -2.11 → {format_pct(-2.11)} [OK]")

    assert format_pct(0) == "+0.00%", f"实际: {format_pct(0)}"
    print(f"  0 → {format_pct(0)} [OK]")

    print("[OK] format_pct 正确\n")


def test_format_market_cap():
    print("=" * 60)
    print("TC-Common-11: format_market_cap 市值格式化")
    print("=" * 60)

    assert format_market_cap(2100000) == "210.00亿", f"实际: {format_market_cap(2100000)}"
    print(f"  2100000 → {format_market_cap(2100000)} [OK]")

    assert format_market_cap(5000) == "5000万", f"实际: {format_market_cap(5000)}"
    print(f"  5000 → {format_market_cap(5000)} [OK]")

    print("[OK] format_market_cap 正确\n")


if __name__ == "__main__":
    try:
        test_validate_stock_code()
        test_resolve_stock_from_names()
        test_resolve_stock_from_codes()
        test_resolve_stock_empty()
        test_resolve_stocks_multiple()
        test_resolve_stocks_max_count()
        test_resolve_sector()
        test_resolve_sector_empty()
        test_format_amount()
        test_format_pct()
        test_format_market_cap()
        print("=" * 60)
        print("[PASS] test_scenario_common 全部通过")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
