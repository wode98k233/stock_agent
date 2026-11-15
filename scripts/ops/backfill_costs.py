"""历史 LLM 步骤的成本回填（成本 / 计价 §11#9）。

上线前产生的 steps 只有 token_usage、没有 cost。本脚本按**当前价目**把它们补上，
并明确标注 `recomputed=True`（说明「这是事后按现价估算，不是当时的真实账单」）。

⚠️ 三条纪律：
1. **默认 dry-run**：不带 --apply 只统计与试算，绝不写库；
2. **写前备份**：--apply 时先把月库文件复制到 data/backups/（文件名带时间戳）；
3. **只补不覆盖**：已有 cost 的步骤一律跳过（历史快照优先，绝不拿现价改写旧账）。

用法：
    venv/Scripts/python.exe scripts/ops/backfill_costs.py                # 预演
    venv/Scripts/python.exe scripts/ops/backfill_costs.py --apply        # 真写
    venv/Scripts/python.exe scripts/ops/backfill_costs.py --days 7 --apply
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # 中文日志在 Windows 控制台不再炸（GBK）
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


def _ts_of(value: str) -> float:
    """started_at（UTC ISO）→ unix 时间戳；解析失败用当前时刻。"""
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return datetime.now(timezone.utc).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser(description="回填历史 LLM 步骤的成本（默认预演）")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预演）")
    ap.add_argument("--days", type=int, default=28, help="只回填最近 N 天（默认 28，与大屏窗口对齐）")
    ap.add_argument("--db", default="", help="只处理指定月库文件（默认扫描全部月库）")
    args = ap.parse_args()

    from utils import pricing
    from utils.agent_trace.store import store

    catalog = pricing.load_catalog(force=True)
    print("[价目] 版本 %s · 供应商 %s" % (catalog.get("version"), list((catalog.get("providers") or {}).keys())))

    since = (datetime.now(timezone.utc) - timedelta(days=max(1, args.days))).isoformat()
    if args.db:
        paths = [Path(args.db)]
    else:
        paths = [store.month_db_path(m) for m in store.iter_existing_months()]

    total_steps = filled = skipped_has_cost = skipped_no_usage = 0
    total_cost = 0.0
    backups: list[str] = []

    for path in paths:
        if not Path(path).exists():
            continue
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, extra, started_at FROM steps "
                "WHERE step_type='llm' AND extra IS NOT NULL AND started_at>=?",
                (since,),
            ).fetchall()
            updates = []
            for row in rows:
                total_steps += 1
                try:
                    extra = json.loads(row["extra"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if extra.get("cache_hit"):
                    continue
                if isinstance(extra.get("cost"), dict):
                    skipped_has_cost += 1
                    continue
                usage = extra.get("token_usage")
                if not isinstance(usage, dict) or not (usage.get("input_tokens") or usage.get("output_tokens")):
                    skipped_no_usage += 1
                    continue
                cost = pricing.compute_cost(
                    usage,
                    model=str(extra.get("model_name") or ""),
                    base_url=pricing.default_base_url(),
                    ts=_ts_of(row["started_at"]),
                )
                if cost is None:
                    continue
                payload = cost.as_dict()
                payload["recomputed"] = True      # 事后按现价估算，不是当时账单
                extra["cost"] = payload
                updates.append((json.dumps(extra, ensure_ascii=False, default=str), row["id"]))
                filled += 1
                if payload.get("total"):
                    total_cost += float(payload["total"])

            if updates and args.apply:
                backup_dir = ROOT / "data" / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = backup_dir / (Path(path).name + "." + stamp + ".pre-cost-backfill.bak")
                shutil.copy2(path, backup)
                backups.append(str(backup))
                conn.executemany("UPDATE steps SET extra=? WHERE id=?", updates)
                conn.commit()
        finally:
            conn.close()

    print("[范围] 最近 %d 天 · %d 个库文件 · 扫到 llm 步骤 %d 条" % (args.days, len(paths), total_steps))
    print("[结果] 待回填 %d 条 · 已有成本跳过 %d 条 · 无 token 跳过 %d 条" % (filled, skipped_has_cost, skipped_no_usage))
    print("[金额] 回填金额合计 ≈ ¥%.4f（按当前价目估算）" % total_cost)
    if args.apply:
        print("[写入] 已写 %d 条；备份：%s" % (filled, "、".join(backups) or "无"))
    else:
        print("[预演] 未写库。确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
