#!/usr/bin/env python3
"""
直接测试 baostock 接口，不通过 fetcher 包装层
验证是否真的超时
"""
import sys
import os
import time
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_baostock_login():
    """测试 baostock 登录"""
    print("=" * 60)
    print("测试 1: baostock 登录")
    print("=" * 60)
    
    try:
        import baostock as bs
        print("  导入 baostock 成功")
        
        t0 = time.time()
        lg = bs.login()
        elapsed = time.time() - t0
        
        if lg.error_code == '0':
            print(f"  ✅ 登录成功，耗时: {elapsed:.2f}s")
            return True, bs
        else:
            print(f"  ❌ 登录失败: {lg.error_code} - {lg.error_msg}")
            return False, None
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False, None


def test_history_kline(bs):
    """测试历史K线"""
    print("\n" + "=" * 60)
    print("测试 2: 历史K线 (query_history_k_data_plus)")
    print("=" * 60)
    
    try:
        symbol = '600519'
        bs_sym = f'sh.{symbol}'
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        
        print(f"  查询: {bs_sym} {start} ~ {end}")
        
        t0 = time.time()
        rs = bs.query_history_k_data_plus(
            bs_sym, "date,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end, frequency='d', adjustflag="3"
        )
        elapsed = time.time() - t0
        
        if rs.error_code == '0':
            print(f"  ✅ 查询成功，耗时: {elapsed:.2f}s")
            
            # 读取数据
            data = []
            while (rs.error_code == '0') & rs.next():
                data.append(rs.get_row_data())
            
            if data:
                df = pd.DataFrame(data, columns=rs.fields)
                print(f"  数据行数: {len(df)}")
                print(f"  列: {list(df.columns)}")
                print(f"  最新一行: {df.tail(1).to_dict('records')[0]}")
                return True
            else:
                print("  ⚠️  无数据")
                return True
        else:
            print(f"  ❌ 查询失败: {rs.error_code} - {rs.error_msg}")
            return False
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_industry_list(bs):
    """测试行业板块列表"""
    print("\n" + "=" * 60)
    print("测试 3: 行业板块列表 (query_stock_industry)")
    print("=" * 60)
    
    try:
        t0 = time.time()
        rs = bs.query_stock_industry()
        elapsed = time.time() - t0
        
        if rs.error_code == '0':
            print(f"  ✅ 查询成功，耗时: {elapsed:.2f}s")
            
            # 读取数据
            data = []
            while (rs.error_code == '0') & rs.next():
                data.append(rs.get_row_data())
            
            if data:
                df = pd.DataFrame(data, columns=rs.fields)
                print(f"  原始数据行数: {len(df)}")
                print(f"  列: {list(df.columns)}")
                
                if 'industry' in df.columns:
                    industries = df[df['industry'].str.strip().astype(bool)][['industry']].drop_duplicates()
                    print(f"  去重后行业数: {len(industries)}")
                    print(f"  前5个行业: {list(industries['industry'].head())}")
                return True
            else:
                print("  ⚠️  无数据")
                return True
        else:
            print(f"  ❌ 查询失败: {rs.error_code} - {rs.error_msg}")
            return False
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_industry_cons(bs):
    """测试行业板块成分股"""
    print("\n" + "=" * 60)
    print("测试 4: 行业板块成分股")
    print("=" * 60)
    
    try:
        t0 = time.time()
        rs = bs.query_stock_industry()
        elapsed = time.time() - t0
        
        if rs.error_code == '0':
            print(f"  ✅ 查询成功，耗时: {elapsed:.2f}s")
            
            # 读取数据
            data = []
            while (rs.error_code == '0') & rs.next():
                data.append(rs.get_row_data())
            
            if data:
                df = pd.DataFrame(data, columns=rs.fields)
                print(f"  原始数据行数: {len(df)}")
                
                # 筛选一个行业
                if 'industry' in df.columns:
                    # 找第一个有数据的行业
                    keyword = '电力'
                    matched = df[
                        df['industry'].str.contains(keyword, na=False) |
                        df['code_name'].str.contains(keyword, na=False)
                    ]
                    
                    if not matched.empty:
                        print(f"  匹配到 '{keyword}' 的股票数: {len(matched)}")
                        print(f"  前5只: {list(matched[['code', 'code_name']].head().to_dict('records'))}")
                    else:
                        print(f"  ⚠️  未匹配到 '{keyword}'")
                return True
            else:
                print("  ⚠️  无数据")
                return True
        else:
            print(f"  ❌ 查询失败: {rs.error_code} - {rs.error_msg}")
            return False
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_financial_abstract(bs):
    """测试财务摘要"""
    print("\n" + "=" * 60)
    print("测试 5: 财务数据")
    print("=" * 60)
    
    try:
        symbol = '600519'
        bs_sym = f'sh.{symbol}'
        y = datetime.now().year - 1
        
        print(f"  查询: {bs_sym} {y}年")
        
        # 测试盈利能力
        print("\n  1. 盈利能力 (query_profit_data)")
        t0 = time.time()
        rs = bs.query_profit_data(code=bs_sym, year=y, quarter=4)
        elapsed = time.time() - t0
        
        if rs.error_code == '0':
            print(f"    ✅ 查询成功，耗时: {elapsed:.2f}s")
            data = []
            while (rs.error_code == '0') & rs.next():
                data.append(rs.get_row_data())
            if data:
                df = pd.DataFrame(data, columns=rs.fields)
                print(f"    数据: {df.to_dict('records')}")
        else:
            print(f"    ❌ 查询失败: {rs.error_code} - {rs.error_msg}")
        
        return True
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "baostock 直接测试" + " " * 30 + "║")
    print("╚" + "═" * 58 + "╝")
    
    # 登录
    login_ok, bs = test_baostock_login()
    if not login_ok or not bs:
        print("\n❌ 登录失败，退出测试")
        return 1
    
    results = {}
    
    # 测试各个接口
    results['历史K线'] = test_history_kline(bs)
    results['行业列表'] = test_industry_list(bs)
    results['行业成分'] = test_industry_cons(bs)
    results['财务数据'] = test_financial_abstract(bs)
    
    # 登出
    print("\n" + "=" * 60)
    print("登出 baostock")
    print("=" * 60)
    try:
        lg = bs.logout()
        if lg.error_code == '0':
            print("  ✅ 登出成功")
        else:
            print(f"  ⚠️  登出: {lg.error_code} - {lg.error_msg}")
    except:
        pass
    
    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
    
    passed = sum(1 for v in results.values() if v)
    print(f"\n通过: {passed}/{len(results)}")
    
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
