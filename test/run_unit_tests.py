#!/usr/bin/env python3
"""
运行所有单元测试（通过 pytest）
单元测试只测试纯逻辑，不需要网络连接
"""
import subprocess
import sys
import os

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    """运行单元测试"""
    print("\n" + "=" * 60)
    print("运行单元测试 (Unit Tests via pytest)")
    print("=" * 60 + "\n")

    # 运行 pytest
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "test/unit/",
            "-v",
            "-m", "unit or not integration",  # 只运行标记为 unit 或未标记的测试
            "--tb=short",
        ],
        cwd=PROJECT_ROOT,
        capture_output=False,
    )

    if result.returncode == 0:
        print("\n" + "=" * 60)
        print("[OK] 所有单元测试通过!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("[FAIL] 部分单元测试失败")
        print("=" * 60)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
