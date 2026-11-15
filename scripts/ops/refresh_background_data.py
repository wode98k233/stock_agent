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
    python scripts/ops/refresh_background_data.py --jobs 4                  # 并发跑（默认不推荐，见下）
    python scripts/ops/refresh_background_data.py --list                    # 只列出作业不执行
    python scripts/ops/refresh_background_data.py --skip-thesis-charts      # 跳过双轴图预热
    python scripts/ops/refresh_background_data.py --thesis-charts-only      # 只预热双轴图（作业不跑）

双轴图预热（--skip-thesis-charts / --thesis-charts-only）：
    作业跑完后逐张调用 `server.thesis_service.get_chart_data(tid, force=True, offline=True)`，
    把 8 张台账的双轴图先算好。⚠️ 本脚本是**独立进程，与 web server 不共享内存**：
    预热结果必须写进 L2 持久缓存（`cache_kv` 的 `cache_thesis_chart:*`）服务器才能
    复用，所以这里先 `ensure_cache_tables()`，再由 force=True 强制写入 L2。
    **offline=True**：只读本地库（不补数、不抓估值）——本函数紧跟在 `thesis_refresh`
    之后，数据刚拉过；再联网会让每张多花 26~65s 且白耗配额。实测 8 张 ~344s → 个位数秒。
    force=True 只跳过 L1/L2、仍复用 L3 因子缓存：重复预热时只有输入轴前进的因子重放。

并发说明（--jobs）：
    代码层面并发安全（ScanManager.run 自带 per-job 锁、存储层单例+WAL+RLock、
    限流器自带锁），但**实测不建议开启**：串行 243.6s vs --jobs 4 的 330.4s，
    反而慢 35% 且触发 429（上游配额共享，并发互相抢；限流退避 30s 的代价
    远大于并行省的墙钟）。瓶颈在 thesis_refresh 单个作业内部，不是作业间等待。
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


def _run_one(mgr, key: str) -> dict:
    """在线程里跑一个作业，异常收敛成 res（不让线程把主流程带崩）。"""
    started = time.time()
    try:
        res = mgr.run(key, "manual")
    except Exception as e:  # noqa: BLE001
        res = {"key": key, "ok": False, "error": f"{type(e).__name__}: {e}",
               "summary": "执行失败：" + str(e)[:200], "duration_ms": 0}
    res.setdefault("duration_ms", int((time.time() - started) * 1000))
    return res


def run_jobs_parallel(mgr, targets: list[str], max_workers: int) -> dict:
    """并发跑多个作业，按 key 返回结果字典。

    ⚠️ **实测不建议开启**（2026-10-19，本机 7 作业全量）：
        串行 243.6s → ``--jobs 4`` 330.4s，**反而慢 35%**，且新增失败：
        - shortline 触发 429 限流，等 30s 重试（10.6s → 71.8s）
        - 市场面板异动拉取 429 降级（fuyao.aicubes.cn）
    原因是**上游数据源是共享配额**，各作业并非真的互不相关：thesis 的宏观指标、
    shortline、市场面板都要打同一批上游，并发只是互相抢配额并触发退避重试，
    而限流退避的代价（30s）远大于并行省下的墙钟。
    真正的瓶颈是 thesis_refresh 单个作业内部（见 thesis 台账的 akshare 超时），
    作业级并行治不了它。

    保留本函数仅供「确认上游有富余配额」时手动使用；默认走串行。
    """
    from concurrent.futures import ThreadPoolExecutor

    results: dict[str, dict] = {}
    print(f"并发执行（{max_workers} 线程）: {', '.join(targets)}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {key: pool.submit(_run_one, mgr, key) for key in targets}
        # 按原顺序等待/收集，保证输出稳定可读（实际是并发的，只是打印顺序固定）
        for key in targets:
            res = futures[key].result()
            results[key] = res
            print(f"\n[{key}] ({res.get('duration_ms', 0) / 1000:.1f}s)")
            for line in _summary_lines(res):
                print(line)
    return results


def run_jobs(only: list[str] | None, skip_market: bool, max_workers: int = 1) -> int:
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

    t_start = time.time()
    if max_workers > 1 and len(targets) > 1:
        results = run_jobs_parallel(mgr, targets, max_workers)
    else:
        results = {}
        for key in targets:
            started = time.time()
            res = mgr.run(key, "manual")
            print(f"\n[{key}] ({time.time() - started:.1f}s)")
            for line in _summary_lines(res):
                print(line)
            results[key] = res
    for key in targets:
        if not results[key].get("ok"):
            failures += 1

    # 分子分母必须同一口径：面板成功也要计入成功数，否则全绿也显示 7/8
    succeeded = len(targets) - failures
    total = len(targets)
    if not skip_market:
        total += 1
        print("\n[market_dashboard] 市场面板快照预热…")
        started = time.time()
        try:
            from server import market_dashboard_service as svc
            snap = svc.build_snapshot()
            elapsed = time.time() - started
            if snap.get("enabled"):
                print(f"  完成 ({elapsed:.1f}s) sentiment_updated_at="
                      f"{snap.get('sentiment_updated_at')} degraded={snap.get('degraded')}")
                succeeded += 1
            else:
                # 面板被开关关掉 = 有意跳过，不计入失败
                print(f"  跳过 ({elapsed:.1f}s): MARKET_DASHBOARD_ENABLED=false")
                total -= 1
        except Exception as e:  # noqa: BLE001
            print(f"  失败: {e}")
            failures += 1

    print(f"\n完成: {succeeded}/{total} 成功 · 总耗时 {time.time() - t_start:.1f}s")
    return 0 if failures == 0 else 1


def prewarm_thesis_charts() -> int:
    """预热 8 张台账的双轴复盘图，返回失败张数（单张失败不中断整体）。

    ⚠️ **独立进程必须写 L2**：本脚本不经过 web server，进程内 L1 缓存在脚本退出
    后就没了；只有 `get_chart_data(force=True)` 写进 `cache_kv` 的 L2
    （`cache_thesis_chart:{tid}:{def_hash}:{h}`）服务器才看得到。所以这里显式
    `ensure_cache_tables()`（脚本没有 bootstrap 的建表流程）。

    **offline=True**：只读本地库，不补数、不抓指数估值。本函数在 `thesis_refresh`
    作业之后调用，数据刚拉过；再联网会让每张图多花 26~65s（多是超时）并白耗上游
    配额。实测 8 张从 ~344s 降到个位数秒。代价：本地库缺数据的因子会被判 dropped，
    不会现场补——这正是「预热」该有的行为（补数由前面的作业负责）。

    force=True 跳过 L1/L2，但仍复用 L3 因子缓存（`cache_thesis_factor:*`）：因子
    输入轴没前进的不会重放（仍是「只算新增/必要部分」）。

    失败（本地库缺数、单张异常）只打印并继续——预热是尽力而为的缓存优化，不能让
    一张图画不出来拖垮整轮后台刷新。
    """
    from utils.cache.core import ensure_cache_tables

    started_all = time.time()
    try:
        ensure_cache_tables()           # 独立进程没有 bootstrap，自己确保 cache_kv 存在
        from server import thesis_service
        items = thesis_service.list_theses()
    except Exception as e:              # noqa: BLE001 - 定义目录/建表失败只影响预热
        print(f"双轴图预热失败：{type(e).__name__}: {e}")
        return 1

    print(f"双轴图预热 {len(items)} 张…")
    failed = 0
    for it in items:
        tid = (it or {}).get("thesis_id")
        if not tid:
            continue
        started = time.time()
        try:
            data = thesis_service.get_chart_data(tid, force=True, offline=True)
            rng = data.get("range") or []
            print(f"  {tid}: 完成 ({time.time() - started:.1f}s) n={data.get('n')} "
                  f"区间 {rng[0] if rng else '?'}~{rng[-1] if len(rng) > 1 else '?'} "
                  f"丢弃 {len(data.get('dropped') or [])} 张因子")
        except Exception as e:          # noqa: BLE001 - 单张失败不中断
            failed += 1
            print(f"  {tid}: 失败 ({time.time() - started:.1f}s) "
                  f"{type(e).__name__}: {e}")
    print(f"双轴图预热完成：{len(items) - failed}/{len(items)} 成功 · "
          f"耗时 {time.time() - started_all:.1f}s")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="刷新 bootstrap 后台数据（短线情绪/台账/拥挤度/存储监控/市场面板），不启动 web。")
    parser.add_argument("--only", nargs="*", default=None,
                        help="只跑指定作业（默认全部）。可选值见 --list。")
    parser.add_argument("--skip-market", action="store_true",
                        help="跳过市场面板快照预热")
    parser.add_argument("--skip-thesis-charts", action="store_true",
                        help="跳过双轴复盘图预热（默认作业跑完后逐张预热 8 张台账）")
    parser.add_argument("--thesis-charts-only", action="store_true",
                        help="只预热双轴复盘图，不跑任何作业/市场面板快照")
    parser.add_argument("--jobs", type=int, default=1, metavar="N",
                        help="并发线程数（默认 1 = 串行）。>1 时作业并发执行，"
                             "总耗时约等于最慢的那个作业。例：--jobs 4")
    parser.add_argument("--list", action="store_true",
                        help="只列出作业定义，不执行")
    args = parser.parse_args()

    if args.list:
        for j in list_jobs():
            state = "启用" if j.enabled else "禁用"
            print(f"{j.key}  [{state}]  {j.name}  (间隔 {j.interval_minutes}min, "
                  f"首延迟 {j.first_delay_s}s)\n     {j.description}")
        return 0

    if args.thesis_charts_only:
        # 只预热图：退出码只由预热结果决定（这是本次唯一的工作）
        return 0 if prewarm_thesis_charts() == 0 else 1

    code = run_jobs(args.only, args.skip_market, max(1, args.jobs))
    # 作业成功后预热双轴图；图表失败只告警，不改退出码（作业刷新才是本职，
    # 图没预热好只是「用户第一次点开要多等几秒」，不应让 cron 报整轮失败）
    if code == 0 and not args.skip_thesis_charts:
        failed = prewarm_thesis_charts()
        if failed:
            print(f"提示：{failed} 张双轴图未预热成功（不影响作业结果，"
                  f"用户首次打开会现场冷算）")
    elif code != 0 and not args.skip_thesis_charts:
        print("\n作业未全部成功，跳过双轴图预热。")
    return code


if __name__ == "__main__":
    sys.exit(main())