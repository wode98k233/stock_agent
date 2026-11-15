# -*- coding: utf-8 -*-
"""信号评测页性能计时脚本（**默认只读**，不写库、不补数、不发网络请求）。

为什么要这个脚本：信号评测页 `Promise.all` 打 8 个接口，页面必须等最慢的一个。
优化前后要能**在同一台机器上复算**冷/热数字，而不是只留一句「从 11.5s 降到 1s」。

口径（逐条钉死，避免「同一个接口有十种计时法」）：

  ① **冷** = 清空全部模块级缓存后，按页面顺序逐接口调用一次（单进程串行）。
     真实页面是 8 路并发，串行数字只用于**改动前后同口径对比**；并发墙钟另见
     场景 B（8 线程同时起跑，全部等完再停表）。
  ② **热** = 紧接冷跑一次，不清任何缓存 —— 这就是用户第二次打开页面的路径。
  ③ `bench_mapping` 单独一行：先清内存映射缓存再计时（磁盘快照存在 → 命中文件，
     不存在 → 全量构建）。**只读**：不删任何文件，想测「真冷」自己先删掉
     `db/bench_mapping.json`。
  ④ 只读开关：把 `_ensure_bench_fresh` patch 成 no-op，避免计时脚本触发基准K线
     增量补数（那会写 market_data.db、还会联网）。因此本脚本的冷数字**不含**
     「基准K线补数」的耗时，那属于数据管道，不是页面打开路径。

用法：
    .\\venv\\Scripts\\python.exe scripts\\devtools\\measure_signal_eval.py
    .\\venv\\Scripts\\python.exe scripts\\devtools\\measure_signal_eval.py --repeat 3
    .\\venv\\Scripts\\python.exe scripts\\devtools\\measure_signal_eval.py --concurrent-only
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ms(fn) -> tuple[float, object]:
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


def _snapshot_status() -> str:
    """磁盘快照状态（是否存在 / 多久前 / 多少条）——只读探测，不修改。"""
    try:
        from config import Config
        path = Path(Config.get_market_data_db_path()).parent / "bench_mapping.json"
    except Exception as e:  # pragma: no cover - 只在配置异常时
        return f"无法定位（{e}）"
    if not path.exists():
        return f"缺失（{path}）→ 本次冷跑会全量构建"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age_h = (time.time() - float(payload.get("ts") or 0)) / 3600
        return (f"命中 {path}  ts={age_h:.1f}h 前  n={len(payload.get('mapping') or {})}")
    except Exception as e:
        return f"损坏（{path}: {e}）→ 本次冷跑会重建"


def _cases(svc, se) -> list[tuple[str, object]]:
    """页面 8 个接口 + 读取/映射，顺序对齐前端首次打开（映射构建在 overview 之前完成）。"""
    import signal_eval.benchmarks as bm

    return [
        ("bench_mapping", lambda: bm.build_bench_mapping()),
        ("load_signals", lambda: svc._load_signals()),
        ("overview", lambda: svc.build_overview(0.0)),
        ("matrix", lambda: se._matrix_sync(0.0)),
        ("signals_200", lambda: se._signals_sync(None, 1, 200, 0.0, False)),
        ("top", lambda: se._top_sync(0.0)),
        ("portfolio", lambda: se._portfolio_sync(0.0)),
        ("calibration", lambda: svc.build_calibration(0.0)),
        ("conditional", lambda: svc.build_conditional(0.0)),
        ("unrecognized", lambda: se._unrecognized_sync(1, 50)),
    ]


def _run_serial(cases) -> tuple[dict[str, float], float]:
    out: dict[str, float] = {}
    wall = 0.0
    for name, fn in cases:
        elapsed, _ = _ms(fn)
        out[name] = elapsed
        wall += elapsed
    return out, wall


def _run_concurrent(cases) -> tuple[dict[str, float], float]:
    """8 路并发（页面同款）：全部线程就位后同时起跑，等最慢的一个。"""
    barrier = threading.Barrier(len(cases))
    elapsed: dict[str, float] = {}

    def run(name, fn):
        barrier.wait()
        t0 = time.perf_counter()
        fn()
        elapsed[name] = (time.perf_counter() - t0) * 1000

    threads = [threading.Thread(target=run, args=(n, f), daemon=True) for n, f in cases]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return elapsed, (time.perf_counter() - t0) * 1000


def _fmt_row(name: str, cold: float, hot: float) -> str:
    speed = (cold / hot) if hot else 0.0
    return f"{name:16s} {cold:9.1f} {hot:9.1f} {speed:7.1f}x"


def main() -> int:
    parser = argparse.ArgumentParser(description="信号评测页冷/热计时（只读）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="冷/热场景重复次数，取中位数（默认 1）")
    parser.add_argument("--concurrent-only", action="store_true",
                        help="只跑 8 路并发场景（不跑串行表）")
    args = parser.parse_args()

    import signal_eval.benchmarks as bm
    from server import signal_eval_service as svc
    from server.routes import signal_eval as se

    # 只读：不触发基准K线增量补数（写库/联网），alias 在模块调用时解析，赋值即生效
    svc._ensure_bench_fresh = lambda *a, **k: None

    cases = _cases(svc, se)
    print(f"bench_mapping 磁盘快照：{_snapshot_status()}")
    print(f"缓存口径：_CACHE_TTL={svc._CACHE_TTL}s  _RESULT_TTL={svc._RESULT_TTL}s"
          f"  信号行数={len(svc._load_signals())}")

    cold_runs: list[dict[str, float]] = []
    hot_runs: list[dict[str, float]] = []
    for _ in range(max(1, args.repeat)):
        # 冷：清内存映射 + 清全部统计缓存（模拟新进程；磁盘快照保留=真实冷启动）
        bm.reset_cache()
        svc.reset_caches()
        cold, _cold_wall = _run_serial(cases)
        # 热：不清任何缓存，第二次打开
        hot, _hot_wall = _run_serial(cases)
        cold_runs.append(cold)
        hot_runs.append(hot)

    def median_of(runs: list[dict[str, float]], name: str) -> float:
        return statistics.median(r[name] for r in runs)

    if not args.concurrent_only:
        print("\n=== 串行（单进程逐接口；用于前后同口径对比） ===")
        print(f"{'case':16s} {'cold(ms)':>9s} {'hot(ms)':>9s} {'加速':>8s}")
        for name, _fn in cases:
            print(_fmt_row(name, median_of(cold_runs, name), median_of(hot_runs, name)))
        print(f"{'total':16s} {sum(median_of(cold_runs, n) for n, _ in cases):9.1f} "
              f"{sum(median_of(hot_runs, n) for n, _ in cases):9.1f}")

    # 场景 B：8 路并发（页面真实路径）。冷 = 清缓存后同时起跑；热 = 紧接着再来一次。
    conc_cold, conc_hot = [], []
    for _ in range(max(1, args.repeat)):
        bm.reset_cache()
        svc.reset_caches()
        conc_cold.append(_run_concurrent(cases))
        conc_hot.append(_run_concurrent(cases))

    print("\n=== 8 路并发（页面同款；页面等待 = 最慢一个 = 墙钟） ===")
    print(f"{'case':16s} {'cold(ms)':>9s} {'hot(ms)':>9s}")
    for name, _fn in cases:
        print(f"{name:16s} "
              f"{statistics.median(r[name] for r, _w in conc_cold):9.1f} "
              f"{statistics.median(r[name] for r, _w in conc_hot):9.1f}")
    print(f"{'wall':16s} "
          f"{statistics.median(w for _r, w in conc_cold):9.1f} "
          f"{statistics.median(w for _r, w in conc_hot):9.1f}")
    print("\n注意：串行/并发均为单进程内计时，不含前端 JS 解析与 HTTP 往返；"
          "只读模式已关闭基准K线补数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
