"""
递归清理项目中所有 __pycache__ 文件夹
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils.app_paths import get_app_dir


def clean_pycache():
    project_root = get_app_dir()
    removed_count = 0
    freed_bytes = 0

    for dirpath, dirnames, filenames in os.walk(project_root):
        if '__pycache__' in dirnames:
            cache_dir = os.path.join(dirpath, '__pycache__')
            try:
                size = sum(
                    os.path.getsize(os.path.join(cache_dir, f))
                    for f in os.listdir(cache_dir)
                    if os.path.isfile(os.path.join(cache_dir, f))
                )
                shutil.rmtree(cache_dir)
                rel = os.path.relpath(cache_dir, project_root)
                freed_bytes += size
                removed_count += 1
                print(f"  已删除: {rel} ({_fmt_size(size)})")
            except Exception as e:
                rel = os.path.relpath(cache_dir, project_root)
                print(f"  删除失败: {rel} - {e}")

    print(f"\n清理完成: 删除 {removed_count} 个 __pycache__ 目录, 释放 {_fmt_size(freed_bytes)}")


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


if __name__ == "__main__":
    print("=" * 50)
    print("清理 __pycache__ 目录")
    print("=" * 50)
    clean_pycache()
