"""一键刷新 bootstrap 后台数据，不启动 web server。

复用与 web 完全相同的作业定义与执行链路，webserver 侧新增/调整作业后本脚本自动跟随：
- 作业定义：`server.scan_jobs.default_jobs()`（单一事实源，bootstrap 注册的同一份）
- 执行器：`server.background_scans.ScanManager.run()`（与 `/api/scans/run/{key}` 同一条链路）
- 市场面板快照：`server.market_dashboard_service.build_snapshot()`（bootstrap
  `_schedule_market_dashboard_prewarm` 的同款调用）

用法：
    python scripts/ops/refresh_background_data.py                            # 跑全部默认作业
    python scripts/ops/refresh_background_data.py --only shortline_refresh  # 只跑短线情绪
    python scripts/ops/refresh_background_data.py --skip-market             # 跳过市场面板快照
    python scripts/ops/refresh_background_data.py --list                    # 只列出作业不执行
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _summary_lines(res: dict) -> list[str]:
    lines = [f"  {res.get('key')}: ok={res.get('ok')} 用时 {res.get('duration_ms', 0) / 1000:.1f}s"]
    if res.get("skipped"):
        lines.append(f"    → 跳过: {res.get('summary')}")
    elif res.get("ok"):
        lines.append(f"    → {res.get('summary')}")
    else:
        lines.append(f"    → 失败: {res.get('error')}")
    return lines


def list_jobs() -> list:
    from server.scan_jobs import default_jobs
    return default_jobs()


def run_jobs(only: list[str] | None, skip_market: bool) -> int:
    from server.background_scans import ScanManager
    from server.scan_jobs import register_default_jobs

    mgr = ScanManager()
    register_default_jobs(mgr)
    keys = mgr.keys()

    if only:
        unknown = [k for k in only if k not in keys]
        if unknown:
            print(f"未知作业: {', '.join(unknown)}（可选: {', '.join(keys)}）")
            return 2
        targets = only
    else:
        targets = keys

    print(f"巡检作业 {len(targets)} 个: {', '.join(targets)}")
    failures = 0
    for key in targets:
        started = time.time()
        res = mgr.run(key, "manual")
        print(f"\n[{key}] ({time.time() - started:.1f}s)")
        for line in _summary_lines(res):
            print(line)
        if not res.get("ok"):
            failures += 1

    if not skip_market:
        print("\n[market_dashboard] 市场面板快照预热…")
        started = time.time()
        try:
            from server import market_dashboard_service as svc
            snap = svc.build_snapshot()
            elapsed = time.time() - started
            if snap.get("enabled"):
                print(f"  完成 ({elapsed:.1f}s) sentiment_updated_at="
                      f"{snap.get('sentiment_updated_at')} degraded={snap.get('degraded')}")
            else:
                print(f"  跳过 ({elapsed:.1f}s): MARKET_DASHBOARD_ENABLED=false")
        except Exception as e:  # noqa: BLE001
            print(f"  失败: {e}")
            failures += 1

    print(f"\n完成: {len(targets) - failures}/{len(targets) + (0 if skip_market else 1)} 成功")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="刷新 bootstrap 后台数据（短线情绪/台账/拥挤度/存储监控/市场面板），不启动 web。")
    parser.add_argument("--only", nargs="*", default=None,
                        help="只跑指定作业（默认全部）。可选值见 --list。")
    parser.add_argument("--skip-market", action="store_true",
                        help="跳过市场面板快照预热")
    parser.add_argument("--list", action="store_true",
                        help="只列出作业定义，不执行")
    args = parser.parse_args()

    if args.list:
        for j in list_jobs():
            state = "启用" if j.enabled else "禁用"
            print(f"{j.key}  [{state}]  {j.name}  (间隔 {j.interval_minutes}min, "
                  f"首延迟 {j.first_delay_s}s)\n     {j.description}")
        return 0

    return run_jobs(args.only, args.skip_market)


if __name__ == "__main__":
    sys.exit(main())