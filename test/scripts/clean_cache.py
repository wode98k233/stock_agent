import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.cache import async_clean_expired_cache
if __name__ == "__main__":
    # 异步清理过期缓存
    print("\n🔄 正在清理过期缓存...")
    try:
        asyncio.run(async_clean_expired_cache())
        print("✅ 过期缓存清理完成")
    except Exception as e:
        print(f"⚠️  缓存清理过程中出现错误: {e}")
