"""回填 strategies.strategy_hash：存量「研发→采纳」策略的指纹补写（设计 §8 修 B5）。

背景
----
`_adopt_save` 现在会把最优 iter 的 `strategy_hash` 写进 `strategies`（任务 6 步骤 6.5），
但在此之前采纳的策略（如 `sr_aafde8`）`strategy_hash` 仍是空串，
导致「采纳后的策略 → 研发迭代史」这条反查链断掉。本脚本负责**存量数据**。

匹配口径（保守，可审计）
------------------------
对每个 `strategy_hash` 为空的非内置策略，按两级口径找它的来源 iter：
  1. **ID 口径**：`sr_` + 某个 `strategy_run.run_uuid` 末段小写
     （例：`SR-20260906-AAFDE8` → `sr_aafde8`），取该 run 的 `best_iter_id`；
  2. **名字口径**：`strategies.name` == 某个 `strategy_iter.strategy_json` 里的 `meta.name`；
     多个候选按 iter id 升序取第一个。
两级都匹配不到的策略**不动**（宁缺毋滥，脚本只补有确凿来源的）。

幂等：`strategy_hash` 已非空的一律跳过（不覆盖新逻辑/人工写入的值），重复执行输出 0 变更。

用法：
    python scripts/ops/backfill_strategy_hash.py            # 预演（默认，不写入）
    python scripts/ops/backfill_strategy_hash.py --apply    # 实际写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load_rows():
    """读取 strategies / strategy_run / strategy_iter 三表（只读）。"""
    from utils.cache.backtest_db import get_backtest_db
    with get_backtest_db() as conn:
        strategies = [dict(r) for r in conn.execute(
            "SELECT strategy_id, name, strategy_hash, is_builtin FROM strategies")]
        runs = [dict(r) for r in conn.execute(
            "SELECT id, run_uuid, best_iter_id, status FROM strategy_run")]
        iters = [dict(r) for r in conn.execute(
            "SELECT id, run_id, strategy_json, strategy_hash FROM strategy_iter")]
    return strategies, runs, iters


def _iter_name(iter_row: dict) -> str:
    """取 strategy_json 里的 meta.name（解析失败返回空串）。"""
    try:
        obj = json.loads(iter_row.get("strategy_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return str(((obj.get("meta") or {}).get("name")) or "")


def build_plan(strategies: list, runs: list, iters: list) -> list:
    """算出待回填清单（纯函数，不改库）。"""
    iter_by_id = {it["id"]: it for it in iters}
    name_candidates: dict = {}
    for it in iters:
        nm = _iter_name(it)
        if nm:
            name_candidates.setdefault(nm, []).append(it)

    plan = []
    for st in strategies:
        if st.get("is_builtin"):
            continue
        if (st.get("strategy_hash") or "").strip():
            continue  # 已有指纹：幂等跳过
        source_iter, reason = None, ""
        sid = str(st.get("strategy_id") or "")
        if sid.startswith("sr_"):
            suffix = sid[3:]
            for run in runs:
                if str(run.get("run_uuid") or "").split("-")[-1].lower() != suffix:
                    continue
                if run.get("best_iter_id"):
                    source_iter = iter_by_id.get(run["best_iter_id"])
                    reason = f"ID 口径：run {run['run_uuid']} best_iter_id={run['best_iter_id']}"
                else:
                    # 兜底：该 run 没有 best_iter_id（旧 run / 未收敛）→ 取本 run 第一个有指纹的 iter
                    source_iter = next(
                        (it for it in iters if it.get("run_id") == run.get("id")
                         and (it.get("strategy_hash") or "").strip()), None)
                    if source_iter is not None:
                        reason = (f"ID 口径兜底：run {run['run_uuid']} 无 best_iter_id，"
                                  f"取 iter {source_iter['id']}")
                break
        if source_iter is None:
            cands = sorted(name_candidates.get(str(st.get("name") or ""), []),
                           key=lambda it: it["id"])
            if cands:
                source_iter = cands[0]
                reason = f"名字口径：strategy_iter.id={cands[0]['id']}"
        if source_iter is None:
            continue
        target = (source_iter.get("strategy_hash") or "").strip()
        if not target:
            continue
        plan.append({
            "strategy_id": sid,
            "name": str(st.get("name") or ""),
            "iter_id": source_iter["id"],
            "strategy_hash": target,
            "reason": reason,
        })
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 strategies.strategy_hash（默认预演）")
    parser.add_argument("--apply", action="store_true", help="实际写入（不加则只预演）")
    args = parser.parse_args()

    strategies, runs, iters = _load_rows()
    plan = build_plan(strategies, runs, iters)

    print(f"[backfill_strategy_hash] strategies={len(strategies)} "
          f"strategy_run={len(runs)} strategy_iter={len(iters)}")
    if not plan:
        print("待回填：0 条（幂等：无变更）")
        return 0

    print(f"待回填：{len(plan)} 条")
    for item in plan:
        print(f"  - {item['strategy_id']}  hash={item['strategy_hash']}  "
              f"iter={item['iter_id']}  name={item['name']}  [{item['reason']}]")

    if not args.apply:
        print("预演模式：未写入（加 --apply 实际执行）")
        return 0

    from utils.cache.backtest_db import update_strategy_hash
    ok = 0
    for item in plan:
        if update_strategy_hash(item["strategy_id"], item["strategy_hash"]):
            ok += 1
        else:
            print(f"  ! 未命中行：{item['strategy_id']}")
    print(f"已回填：{ok}/{len(plan)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
