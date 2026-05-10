#!/usr/bin/env python3
"""
测试 baostock 缓存效果
对比第一次（无缓存）和第二次（有缓存）的速度
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_baostock_with_cache():
    """测试 baostock 缓存效果"""
    print("=" * 70)
    print("  测试 baostock 缓存效果")
    print("=" * 70)
    
    # 先导入缓存模块，清空旧缓存
    print("\n1. 准备：清空旧缓存")
    from tools.fetcher.cache import get_baostock_cache
    cache = get_baostock_cache()
    cache.clear_all()
    print("   ✅ 缓存已清空")
    
    # 导入 baostock 数据源
    from tools.fetcher.baostock_ds import BaostockDataSource
    
    print("\n2. 第一次调用（无缓存）")
    t0 = time.time()
    try:
        df_list = BaostockDataSource.get_board_industry_list()
        elapsed1 = time.time() - t0
        print(f"   ✅ 获取行业列表成功")
        print(f"   耗时: {elapsed1:.2f}秒")
        print(f"   行业数: {len(df_list) if df_list is not None else 0}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        elapsed1 = None
    
    print("\n3. 第二次调用（有缓存）")
    t0 = time.time()
    try:
        df_list2 = BaostockDataSource.get_board_industry_list()
        elapsed2 = time.time() - t0
        print(f"   ✅ 获取行业列表成功")
        print(f"   耗时: {elapsed2:.2f}秒")
        print(f"   行业数: {len(df_list2) if df_list2 is not None else 0}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        elapsed2 = None
    
    print("\n4. 测试获取行业成分股（电力）")
    t0 = time.time()
    try:
        df_cons = BaostockDataSource.get_board_industry_cons("电力")
        elapsed3 = time.time() - t0
        print(f"   ✅ 获取电力行业成分股成功")
        print(f"   耗时: {elapsed3:.2f}秒")
        print(f"   股票数: {len(df_cons) if df_cons is not None else 0}")
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        elapsed3 = None
    
    # 总结
    print("\n" + "=" * 70)
    print("  缓存效果总结")
    print("=" * 70)
    
    if elapsed1 is not None and elapsed2 is not None:
        speedup = elapsed1 / elapsed2 if elapsed2 > 0 else float('inf')
        print(f"  第一次（无缓存）: {elapsed1:.2f}秒")
        print(f"  第二次（有缓存）: {elapsed2:.2f}秒")
        print(f"  加速倍数: {speedup:.1f}x")
        print(f"  节省时间: {elapsed1 - elapsed2:.2f}秒")
    else:
        print("  无法计算加速效果（有测试失败）")
    
    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(test_baostock_with_cache())
