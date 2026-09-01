
"""清理缓存并测试评级获取"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO)

from tools.stock_data import get_stock_rating

print("开始清理评级缓存...")

# 清理缓存（cache_kv 中 cache_rating 前缀的条目）
try:
    import sqlite3
    from utils.app_paths import get_market_cache_db_path
    conn = sqlite3.connect(get_market_cache_db_path())
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cache_kv WHERE cache_key LIKE 'cache_rating:%'")
    conn.commit()
    conn.close()
    print("   缓存清理成功")
except Exception as e:
    print(f"   清理缓存失败: {e}")

print("\n开始测试 get_stock_rating...")

symbol = "600519"

try:
    result = get_stock_rating(symbol)
    print(f"\n获取结果:")
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
except Exception as e:
    print(f"出错: {e}")
    import traceback
    print(traceback.format_exc())
