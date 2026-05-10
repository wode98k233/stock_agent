
"""清理缓存并测试评级获取"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO)

from config import Config
from tools.stock_data import get_stock_rating

print("开始清理评级缓存...")

# 清理缓存
try:
    import sqlite3
    conn = sqlite3.connect(Config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cache_rating")
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
