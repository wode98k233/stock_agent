#!/usr/bin/env python3
"""
fetcher 模块全面测试脚本
测试所有数据源 × 所有接口的能力和正确性
"""
import os
import sys
import time
import logging
import traceback
import pandas as pd
import concurrent.futures
from functools import partial

# 日志
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
# 确保能 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ═══════════════════════════════════════════════════════════
#  全局
# ═══════════════════════════════════════════════════════════

TEST_STOCK = '600519'       # 贵州茅台
TEST_STOCK_NAME = '贵州茅台'

# 并行配置
MAX_WORKERS_THREAD = 4  # IO密集型用线程
MAX_WORKERS_PROCESS = 2  # CPU密集型用进程

# 所有需要测试的方法及其描述
METHODS = {
    'get_stock_hist':        '历史K线',
    'get_spot_em':           '实时行情',
    'get_board_industry_list': '行业板块列表',
    'get_board_concept_list':  '概念板块列表',
    'get_board_industry_cons': '行业板块成分股',
    'get_board_concept_cons':  '概念板块成分股',
    'get_stock_news':        '个股新闻',
    'get_stock_rating':      '机构评级',
    'get_financial_abstract': '财务摘要',
}


def divider(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════
#  Test 1: 模块导入
# ═══════════════════════════════════════════════════════════

def test_import():
    divider("TEST 1: 模块导入")
    try:
        from tools.fetcher import (
            ak_stock_hist, ak_spot_em,
            ak_board_industry_cons, ak_board_concept_cons,
            ak_board_industry_list, ak_board_concept_list,
            ak_stock_news, ak_stock_rating, ak_financial_abstract,
            ak_index_daily,
            get_current_data_source, get_available_data_sources,
            switch_data_source, reset_data_source, get_datasource_status,
            DataSource, DataSourceManager,
        )
        print("  ✅ 所有公共 API 导入成功")
        return True
    except Exception as e:
        print(f"  ❌ 导入失败: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════
#  Test 2: 数据源状态
# ═══════════════════════════════════════════════════════════

def test_status():
    divider("TEST 2: 数据源注册状态")
    from tools.fetcher import get_datasource_status, get_available_data_sources

    status = get_datasource_status()
    available = get_available_data_sources()

    print(f"\n  已注册: {len(status)} 个数据源")
    print(f"  可用:   {len(available)} → {available}\n")

    for name, info in status.items():
        icon = "🟢" if info['available'] else "🔴"
        print(f"  {icon} {name:20s}  pri={info['priority']:3d}  "
              f"enabled={str(info['enabled']):5s}  available={str(info['available']):5s}  "
              f"fails={info['fail_count']}")

    if not available:
        print("\n  ⚠️  没有可用的数据源，后续测试将跳过需要数据源的部分")
    return True


# ═══════════════════════════════════════════════════════════
#  Test 3: 逐数据源 × 逐方法 测试
# ═══════════════════════════════════════════════════════════

def _call_method_with_timeout(source, method_name, stock=TEST_STOCK, timeout=60):
    """带超时的方法调用，返回 (ok, detail)"""
    import threading
    
    result = [None, None]  # [ok, detail]
    event = threading.Event()
    
    def _worker():
        try:
            ok, detail = _call_method(source, method_name, stock)
            result[0], result[1] = ok, detail
        except Exception as e:
            result[0], result[1] = False, f"异常: {type(e).__name__}: {e}"
        finally:
            event.set()
    
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    
    if event.wait(timeout=timeout):
        return result[0], result[1]
    else:
        return False, f"超时: 超过 {timeout} 秒，接口异常慢"


def _call_method(source, method_name, stock=TEST_STOCK):
    """调用数据源的某个方法，返回 (ok, detail)"""
    method = getattr(source, method_name, None)
    if method is None:
        return False, "方法不存在"

    try:
        if method_name in ('get_stock_hist', 'get_stock_news',
                           'get_stock_rating', 'get_financial_abstract'):
            df = method(stock)
        elif method_name in ('get_board_industry_cons', 'get_board_concept_cons'):
            # 先从列表中取一个板块名
            list_method = getattr(source, method_name.replace('_cons', '_list'), None)
            board_name = None
            if list_method:
                try:
                    list_df = list_method()
                    if list_df is not None and not list_df.empty:
                        board_name = list_df.iloc[0].get('板块名称', list_df.iloc[0].get('板块代码', ''))
                except:
                    pass
            if not board_name:
                # 用常见板块名兜底
                board_name = '有色金属' if 'industry' in method_name else '融资融券'
            df = method(board_name)
        elif method_name in ('get_board_industry_list', 'get_board_concept_list',
                             'get_spot_em'):
            df = method()
        else:
            df = method()

        if df is None:
            return False, "返回 None"
        if isinstance(df, pd.DataFrame):
            if df.empty:
                return False, "返回空 DataFrame"
            return True, f"{len(df)}行 × {len(df.columns)}列  列={list(df.columns)}"
        return True, f"类型={type(df).__name__}"
    except NotImplementedError as e:
        return False, f"未实现: {e}"
    except Exception as e:
        return False, f"异常: {type(e).__name__}: {e}"


def _test_single_source_method(source_cls, method_name):
    """测试单个数据源的单个方法（用于多线程）"""
    name = source_cls.name
    ok, detail = _call_method_with_timeout(source_cls, method_name, timeout=60)
    return name, method_name, ok, detail


def test_per_source():
    divider("TEST 3: 逐数据源 × 逐方法 测试 (多线程)")

    from tools.fetcher import DataSourceManager
    from tools.fetcher.base import DataSource

    # 收集所有已注册的数据源类
    sources = DataSourceManager._sources
    if not sources:
        print("  ⚠️  没有注册任何数据源")
        return False

    # 结果矩阵
    results = {}  # {source_name: {method_name: (ok, detail)}}
    test_tasks = []

    # 准备所有测试任务
    for source_cls in sources:
        name = source_cls.name
        results[name] = {}

        # 检查可用性
        try:
            avail = source_cls.is_available()
        except:
            avail = False

        if not avail:
            print(f"\n  ── {name} ── 🔴 不可用，全部跳过")
            for m in METHODS:
                results[name][m] = (False, "数据源不可用")
            continue

        print(f"\n  ── {name} ── 🟢 可用，提交 {len(METHODS)} 个测试任务")
        
        # 为每个方法添加测试任务
        for method_name in METHODS:
            test_tasks.append((source_cls, method_name))

    # 多线程执行所有测试任务
    print(f"\n  使用 {MAX_WORKERS_THREAD} 个线程并行执行测试...")
    t0 = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_THREAD) as executor:
        # 提交所有任务
        futures = [executor.submit(_test_single_source_method, src, mtd) 
                  for src, mtd in test_tasks]
        
        # 收集结果
        for future in concurrent.futures.as_completed(futures):
            name, method_name, ok, detail = future.result()
            results[name][method_name] = (ok, detail)
            
            # 显示结果
            desc = METHODS.get(method_name, '')
            icon = "✅" if ok else "❌"
            if "未实现" in detail or "NotImplemented" in detail:
                icon = "⬜"
            print(f"    {icon} {name:15s} {method_name:25s} ({desc})  {detail}")
    
    elapsed = time.time() - t0
    print(f"\n  测试完成，耗时: {elapsed:.1f}s")

    # 汇总表
    divider("TEST 3 汇总: 接口覆盖率")
    print(f"\n  {'数据源':20s}", end='')
    for m, desc in METHODS.items():
        print(f" {m:10s}", end='')
    print()

    for name in results:
        print(f"  {name:20s}", end='')
        for m in METHODS:
            ok, detail = results[name].get(m, (False, ''))
            if ok:
                print(f" {'✅':10s}", end='')
            elif "未实现" in detail or "NotImplemented" in detail:
                print(f" {'⬜':10s}", end='')
            else:
                print(f" {'❌':10s}", end='')
        print()

    return True


# ═══════════════════════════════════════════════════════════
#  Test 4: 统一接口测试（通过 __init__.py 的 retry 装饰器）
# ═══════════════════════════════════════════════════════════

def _test_single_unified_api(name, fn):
    """测试单个统一接口（用于多线程）"""
    try:
        t0 = time.time()
        df = fn()
        elapsed = time.time() - t0
        return name, True, elapsed, df
    except NotImplementedError as e:
        return name, False, 0, str(e)
    except Exception as e:
        return name, False, 0, str(e)


def test_unified_api():
    divider("TEST 4: 统一接口测试（自动故障转移）(多线程)")
    from tools.fetcher import (
        ak_stock_hist, ak_spot_em,
        ak_board_industry_list, ak_board_concept_list,
        ak_board_industry_cons, ak_board_concept_cons,
        ak_stock_news, ak_stock_rating, ak_financial_abstract,
        get_current_data_source, get_available_data_sources,
    )

    available = get_available_data_sources()
    if not available:
        print("  ⚠️  没有可用数据源，跳过统一接口测试")
        return False

    print(f"  可用数据源: {available}")
    print(f"  当前数据源: {get_current_data_source()}")

    unified_tests = [
        ("历史K线", lambda: ak_stock_hist(TEST_STOCK, period="daily")),
        ("行业板块列表", lambda: ak_board_industry_list()),
        ("概念板块列表", lambda: ak_board_concept_list()),
        ("个股新闻", lambda: ak_stock_news(TEST_STOCK)),
        ("机构评级", lambda: ak_stock_rating(TEST_STOCK)),
        ("财务摘要", lambda: ak_financial_abstract(TEST_STOCK)),
    ]

    print(f"\n  使用 {MAX_WORKERS_THREAD} 个线程并行执行测试...")
    t0 = time.time()
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_THREAD) as executor:
        futures = [executor.submit(_test_single_unified_api, name, fn) 
                  for name, fn in unified_tests]
        
        for future in concurrent.futures.as_completed(futures):
            name, ok, elapsed, data = future.result()
            
            if ok:
                df = data
                source = get_current_data_source()
                if df is not None and not df.empty:
                    print(f"  ✅ {name:20s}  source={source:15s} {elapsed:.2f}s  "
                          f"{len(df)}行 × {len(df.columns)}列  列={list(df.columns)}")
                    results[name] = True
                else:
                    print(f"  ⚠️  {name:20s}  source={source:15s} {elapsed:.2f}s  返回空")
                    results[name] = False
            else:
                error_msg = data
                if "未实现" in error_msg or "NotImplemented" in error_msg:
                    print(f"  ⬜ {name:20s}  所有源均未实现")
                else:
                    print(f"  ❌ {name:20s}  失败: {error_msg}")
                results[name] = False
    
    elapsed_total = time.time() - t0
    print(f"\n  测试完成，耗时: {elapsed_total:.1f}s")
    
    passed = sum(1 for v in results.values() if v)
    print(f"\n  统一接口通过: {passed}/{len(results)}")
    return results


# ═══════════════════════════════════════════════════════════
#  Test 5: 数据质量检查
# ═══════════════════════════════════════════════════════════

def _check_historical_data_quality(df):
    """在子进程中检查历史K线数据质量（CPU密集型）"""
    issues = []
    
    if df is None or df.empty:
        return ["历史K线返回空数据"]
    
    required = ['日期', '开盘', '最高', '最低', '收盘']
    missing = [c for c in required if c not in df.columns]
    if missing:
        issues.append(f"历史K线缺少字段: {missing}")
    else:
        # 检查数值合理性
        for c in ['开盘', '最高', '最低', '收盘']:
            vals = pd.to_numeric(df[c], errors='coerce')
            if vals.isna().all():
                issues.append(f"历史K线 '{c}' 全部为 NaN")
            elif (vals <= 0).any():
                issues.append(f"历史K线 '{c}' 存在非正数")
        # 检查 高 >= 低
        high = pd.to_numeric(df['最高'], errors='coerce')
        low = pd.to_numeric(df['最低'], errors='coerce')
        if (high < low).any():
            issues.append("历史K线: 最高 < 最低")
    
    return issues


def _check_board_data_quality(df, name):
    """在子进程中检查板块列表数据质量（CPU密集型）"""
    issues = []
    
    if df is None or df.empty:
        return [f"{name} 返回空数据"]
    
    if '板块名称' not in df.columns and '板块代码' not in df.columns:
        issues.append(f"{name} 缺少 '板块名称' 或 '板块代码' 列，实际: {list(df.columns)}")
    
    return issues


def test_data_quality():
    divider("TEST 5: 数据质量检查（字段 / 类型 / 合理性）(多进程)")
    from tools.fetcher import (
        ak_stock_hist, ak_board_industry_list, ak_board_concept_list,
        get_current_data_source,
    )

    all_issues = []
    
    # 第一步：先获取所有数据（IO密集型，用串行）
    print("\n  第一步: 获取数据 (IO密集型)...")
    data_dict = {}
    
    # 获取历史K线
    try:
        print("    获取历史K线...")
        df_hist = ak_stock_hist(TEST_STOCK, period="daily", start="2025-01-01", end="2025-03-01")
        data_dict['hist'] = df_hist
        if df_hist is not None and not df_hist.empty:
            source = get_current_data_source()
            print(f"      历史K线 (source={source}): {len(df_hist)}行")
            print(f"      列: {list(df_hist.columns)}")
            print(f"      最新行:")
            print(f"      {df_hist.tail(1).to_string(index=False)}")
    except Exception as e:
        print(f"      ❌ 历史K线获取异常: {e}")
        all_issues.append(f"历史K线获取异常: {e}")
        data_dict['hist'] = None
    
    # 获取板块列表
    for method, name in [(ak_board_industry_list, '行业板块列表'),
                          (ak_board_concept_list, '概念板块列表')]:
        try:
            print(f"    获取{name}...")
            df = method()
            data_dict[name] = df
            if df is not None and not df.empty:
                print(f"      {name}: {len(df)}个板块")
        except Exception as e:
            print(f"      ❌ {name}获取异常: {e}")
            data_dict[name] = None
    
    # 第二步：多进程检查数据质量（CPU密集型）
    print(f"\n  第二步: 数据质量检查 (使用 {MAX_WORKERS_PROCESS} 个进程)...")
    t0 = time.time()
    
    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS_PROCESS) as executor:
        futures = []
        
        # 提交历史K线检查
        if data_dict.get('hist') is not None:
            futures.append(executor.submit(_check_historical_data_quality, data_dict['hist']))
        
        # 提交板块列表检查
        for name in ['行业板块列表', '概念板块列表']:
            if data_dict.get(name) is not None:
                futures.append(executor.submit(_check_board_data_quality, data_dict[name], name))
        
        # 收集结果
        for future in concurrent.futures.as_completed(futures):
            issues = future.result()
            all_issues.extend(issues)
    
    elapsed = time.time() - t0
    print(f"  数据质量检查完成，耗时: {elapsed:.1f}s")
    
    # 输出结果
    if all_issues:
        print(f"\n  发现 {len(all_issues)} 个问题:")
        for i in all_issues:
            print(f"    ⚠️  {i}")
    else:
        print(f"\n  ✅ 数据质量检查通过")
    
    return len(all_issues) == 0


# ═══════════════════════════════════════════════════════════
#  Test 6: 故障转移机制
# ═══════════════════════════════════════════════════════════

def test_failover():
    divider("TEST 6: 数据源切换")
    from tools.fetcher import (
        get_current_data_source, get_available_data_sources, switch_data_source,
        DataSourceManager,
    )

    available = get_available_data_sources()
    print(f"  可用数据源: {available}")

    if len(available) < 2:
        print(f"  ⚠️  只有 {len(available)} 个可用源，跳过切换测试")
        return True

    before = get_current_data_source()
    switch_data_source()
    after = get_current_data_source()

    print(f"  切换: {before} → {after}")
    if before != after:
        print(f"  ✅ 切换成功")
    else:
        print(f"  ⚠️  切换后相同（可能 round-robin 回到了同一个）")

    # 切回来
    switch_data_source()
    back = get_current_data_source()
    print(f"  再切换: {after} → {back}")

    return True


# ═══════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║          fetcher 模块全面测试  (数据源 × 接口 × 质量)              ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    test_results = {}

    test_results['导入'] = test_import()
    test_results['数据源状态'] = test_status()
    test_results['逐源测试'] = test_per_source()
    test_results['统一接口'] = test_unified_api()
    test_results['数据质量'] = test_data_quality()
    test_results['数据源切换'] = test_failover()

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