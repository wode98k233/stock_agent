#!/usr/bin/env python3
"""
选股雷达 - CLI 入口
🚀 极简入口，只做初始化和循环调度
"""
import traceback
import asyncio
from cli import bootstrap, router


async def main():
    """🚀 主入口"""
    context = await bootstrap()
    print(context["banner"])

    while True:
        try:
            user_input = input("\n[>>>] ").strip()
            if not user_input:
                continue

            result = await router.handle(user_input, context)
            should_exit = router.output(result)
            if should_exit:
                break

        except KeyboardInterrupt:
            from cli.banner import build_exit_stats
            stats_msg = build_exit_stats(context.get("session_stats"))
            print(f"\n{stats_msg}")
            break
        except Exception as e:
            print(f"[ERROR] {e} {traceback.format_exc()}")


if __name__ == "__main__":
    asyncio.run(main())
