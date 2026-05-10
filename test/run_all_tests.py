#!/usr/bin/env python3
"""
运行所有测试（通过 pytest）
包括单元测试和集成测试
注意：集成测试需要网络连接，可能较慢
"""
import subprocess
import sys
import os

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("运行所有测试 (All Tests via pytest)")
    print("=" * 60 + "\n")

    # 运行 pytest（排除 e2e 测试，那些需要单独运行）
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "test/unit/",
            "test/integration/",
            "-v",
            "--tb=short",
        ],
        cwd=PROJECT_ROOT,
        capture_output=False,
    )

    if result.returncode == 0:
        print("\n" + "=" * 60)
        print("[OK] 所有测试通过!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("[FAIL] 部分测试失败")
        print("=" * 60)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
