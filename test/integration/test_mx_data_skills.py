#!/usr/bin/env python3
"""
mx_data 数据源集成测试
真实 API 调用，验证端到端可用性和列名标准化
需要环境变量 MX_APIKEY
"""
import os
import sys
import time
import logging
import traceback
import pandas as pd

logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_STOCK = '600519'  # 贵州茅台


def divider(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════
#  Test 1: 模块加载和可用性
# ═══════════════════════════════════════════════════════════

def test_availability():
    divider("TEST 1: mx_data 模块加载和可用性")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    if not os.getenv("MX_APIKEY"):
        print("  ⚠️  MX_APIKEY 未配置，跳过所有集成测试")
        return None  # None = skip

    try:
        avail = MXDataDataSource.is_available()
        if avail:
            print("  ✅ mx_data 数据源可用")
        else:
            print("  ❌ mx_data 数据源不可用")
        return avail
    except Exception as e:
        print(f"  ❌ 检查可用性异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 2: 历史K线
# ═══════════════════════════════════════════════════════════

def test_stock_hist():
    divider("TEST 2: 历史K线 (get_stock_hist)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_stock_hist(TEST_STOCK, period="daily",
                                              start="20250101", end="20250301")
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")

        # 验证标准化列名
        required_cols = ['日期', '开盘', '收盘', '最高', '最低']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            print(f"  ❌ 缺少标准化列: {missing}")
            return False
        print(f"  ✅ 标准化列名正确: {required_cols}")

        # 验证不应存在的原始列名
        bad_cols = ['开盘价', '收盘价', '最高价', '最低价', 'date']
        present_bad = [c for c in bad_cols if c in df.columns]
        if present_bad:
            print(f"  ❌ 存在未标准化的原始列: {present_bad}")
            return False
        print(f"  ✅ 无原始列名残留")

        print(f"  最新行:\n{df.tail(1).to_string(index=False)}")
        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 3: 行业板块成分股
# ═══════════════════════════════════════════════════════════

def test_board_industry_cons():
    divider("TEST 3: 行业板块成分股 (get_board_industry_cons)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_board_industry_cons('白酒')
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")

        # 验证标准化列名
        if '代码' not in df.columns or '名称' not in df.columns:
            print(f"  ❌ 缺少标准化列 '代码'/'名称'")
            return False
        print(f"  ✅ 标准化列名正确")

        # 验证不应存在的原始列名
        bad_cols = ['股票代码', '股票名称']
        present_bad = [c for c in bad_cols if c in df.columns]
        if present_bad:
            print(f"  ❌ 存在未标准化的原始列: {present_bad}")
            return False

        print(f"  前3行:\n{df.head(3).to_string(index=False)}")
        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 4: 概念板块成分股
# ═══════════════════════════════════════════════════════════

def test_board_concept_cons():
    divider("TEST 4: 概念板块成分股 (get_board_concept_cons)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_board_concept_cons('人工智能')
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")

        if '代码' not in df.columns or '名称' not in df.columns:
            print(f"  ❌ 缺少标准化列 '代码'/'名称'")
            return False
        print(f"  ✅ 标准化列名正确")
        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 5: 板块列表
# ═══════════════════════════════════════════════════════════

def test_board_lists():
    divider("TEST 5: 板块列表 (get_board_industry_list / get_board_concept_list)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    all_ok = True
    for method_name, desc in [('get_board_industry_list', '行业板块'),
                               ('get_board_concept_list', '概念板块')]:
        try:
            method = getattr(MXDataDataSource, method_name)
            t0 = time.time()
            df = method()
            elapsed = time.time() - t0

            if df is None or df.empty:
                print(f"  ❌ {desc}: 返回空数据 ({elapsed:.1f}s)")
                all_ok = False
                continue

            print(f"  ✅ {desc}: {len(df)}个板块 ({elapsed:.1f}s)  列={list(df.columns)}")
        except Exception as e:
            print(f"  ❌ {desc}: 异常 {e}")
            all_ok = False

    return all_ok


# ═══════════════════════════════════════════════════════════
#  Test 6: 个股新闻
# ═══════════════════════════════════════════════════════════

def test_stock_news():
    divider("TEST 6: 个股新闻 (get_stock_news)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_stock_news(TEST_STOCK)
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")

        # 验证标准化列名
        if '新闻标题' not in df.columns:
            print(f"  ❌ 缺少标准化列 '新闻标题'")
            return False
        print(f"  ✅ 标准化列名正确")

        # 验证不应存在的原始列名
        bad_cols = ['标题', '时间']
        present_bad = [c for c in bad_cols if c in df.columns]
        if present_bad:
            print(f"  ❌ 存在未标准化的原始列: {present_bad}")
            return False

        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 7: 机构评级
# ═══════════════════════════════════════════════════════════

def test_stock_rating():
    divider("TEST 7: 机构评级 (get_stock_rating)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_stock_rating(TEST_STOCK)
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")

        if '机构评级' not in df.columns:
            print(f"  ❌ 缺少标准化列 '机构评级'")
            return False
        print(f"  ✅ 标准化列名正确")
        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 8: 财务摘要
# ═══════════════════════════════════════════════════════════

def test_financial_abstract():
    divider("TEST 8: 财务摘要 (get_financial_abstract)")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    try:
        t0 = time.time()
        df = MXDataDataSource.get_financial_abstract(TEST_STOCK)
        elapsed = time.time() - t0

        if df is None or df.empty:
            print(f"  ❌ 返回空数据 ({elapsed:.1f}s)")
            return False

        print(f"  ✅ 获取成功 ({elapsed:.1f}s): {len(df)}行 × {len(df.columns)}列")
        print(f"  列: {list(df.columns)}")
        print(f"  数据:\n{df.to_string(index=False)}")
        return True
    except Exception as e:
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 9: 列名一致性回归测试
# ═══════════════════════════════════════════════════════════

def test_column_consistency():
    divider("TEST 9: 列名一致性回归测试")
    from tools.fetcher.mx_data_ds import MXDataDataSource

    # 定义期望的标准化列名
    expectations = {
        'get_stock_hist': {
            'must_have': ['日期', '开盘', '收盘', '最高', '最低'],
            'must_not_have': ['开盘价', '收盘价', '最高价', '最低价', 'date'],
        },
        'get_board_industry_cons': {
            'must_have': ['代码', '名称'],
            'must_not_have': ['股票代码', '股票名称'],
        },
        'get_stock_news': {
            'must_have': ['新闻标题'],
            'must_not_have': ['标题', '时间'],
        },
        'get_stock_rating': {
            'must_have': ['机构评级'],
            'must_not_have': ['评级'],
        },
    }

    all_ok = True
    for method_name, checks in expectations.items():
        try:
            method = getattr(MXDataDataSource, method_name)
            if method_name == 'get_stock_hist':
                df = method(TEST_STOCK, start="20250101", end="20250131")
            elif method_name in ('get_board_industry_cons',):
                df = method('白酒')
            else:
                df = method(TEST_STOCK)

            if df is None or df.empty:
                print(f"  ⚠️  {method_name}: 返回空，跳过列名检查")
                continue

            # 检查必须存在的列
            missing = [c for c in checks['must_have'] if c not in df.columns]
            if missing:
                print(f"  ❌ {method_name}: 缺少 {missing}")
                all_ok = False
            else:
                print(f"  ✅ {method_name}: 标准化列名正确")

            # 检查不应存在的列
            bad = [c for c in checks['must_not_have'] if c in df.columns]
            if bad:
                print(f"  ❌ {method_name}: 存在原始列 {bad}")
                all_ok = False

        except Exception as e:
            print(f"  ⚠️  {method_name}: 异常 {e}")

    return all_ok


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║            mx_data 数据源集成测试 (真实 API)                        ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    # 先检查可用性
    avail = test_availability()
    if avail is None:
        print("\n  ⚠️  MX_APIKEY 未配置，所有测试跳过")
        return 0
    if not avail:
        print("\n  ❌ mx_data 不可用，后续测试将全部失败")
        return 1

    test_results = {}

    test_results['历史K线'] = test_stock_hist()
    test_results['行业成分股'] = test_board_industry_cons()
    test_results['概念成分股'] = test_board_concept_cons()
    test_results['板块列表'] = test_board_lists()
    test_results['个股新闻'] = test_stock_news()
    test_results['机构评级'] = test_stock_rating()
    test_results['财务摘要'] = test_financial_abstract()
    test_results['列名一致性'] = test_column_consistency()

    # 总结
    divider("测试总结")
    passed = sum(1 for v in test_results.values() if v)
    total = len(test_results)
    for name, ok in test_results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
    print(f"\n  通过: {passed}/{total}")

    return 0 if all(test_results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
