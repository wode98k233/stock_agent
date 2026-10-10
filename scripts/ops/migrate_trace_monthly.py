"""agent_trace.db 按月分库存量迁移脚本（2026-09 一次性）。

把旧单库 runs/steps/messages 按 runs.created_at 月份拆到 db/agent_trace/agent_trace_YYYYMM.db，
主库瘦身为仅 reviews。幂等可重跑（INSERT OR IGNORE + 逐月事务，中断续传）。

用法：
    # 预览（默认）：打印源库月分布与将执行的动作，不落盘
    venv\\Scripts\\python.exe scripts/ops/migrate_trace_monthly.py

    # 正式执行（需先停 web/CLI，避免并写）
    venv\\Scripts\\python.exe scripts/ops/migrate_trace_monthly.py --apply

流程：
    1) 备份源库（wal_checkpoint(TRUNCATE) 后 copy2 → *.bak-YYYYMMDD-HHMMSS）
    2) 只读打开源库，逐月 ATTACH 目标月库批量复制：
       runs/steps 按 substr(created_at,1,7) 过滤；messages 无时间列，
       经 messages→steps→runs JOIN 补月份归属（孤儿 messages 不可达，随 JOIN 丢弃）
    3) 终态校验：各表源行数（JOIN 口径）= 各月库行数之和
    4) 校验通过后主库 DROP runs/steps/messages + VACUUM（最后一步，保证 .bak 可整体回滚）；
       CWD 残留旧库整体移除（备份副本已存在）
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

TAG = "[trace-migrate]"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.app_paths import get_db_dir, get_trace_dir  # noqa: E402
from utils.agent_trace.models import ensure_month_schema  # noqa: E402


def _month_db_path(ym: str) -> str:
    return str(Path(get_trace_dir()) / f"agent_trace_{ym.replace('-', '')}.db")


def _open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _wal_checkpoint_and_warn(path: str) -> None:
    wal = Path(path + "-wal")
    if wal.exists() and wal.stat().st_mtime > time.time() - 60:
        print(f"{TAG} 警告：{wal.name} 近 60s 内有写入活动，可能服务未停，建议先停 web/CLI")
    if wal.exists():
        c = sqlite3.connect(path)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()


def _months_of(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """源库 runs 的月份分布 [(YYYY-MM, run 数)]。"""
    rows = conn.execute(
        "SELECT substr(created_at,1,7) ym, COUNT(*) n FROM runs GROUP BY ym ORDER BY ym"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _source_counts(conn: sqlite3.Connection, ym: str) -> dict:
    """源库某月份三表行数（messages 走 steps→runs JOIN 口径，与迁移一致）。"""
    runs = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE substr(created_at,1,7)=?", (ym,)
    ).fetchone()[0]
    steps = conn.execute(
        "SELECT COUNT(*) FROM steps WHERE run_id IN "
        "(SELECT id FROM runs WHERE substr(created_at,1,7)=?)", (ym,)
    ).fetchone()[0]
    messages = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE step_id IN ("
        "  SELECT s.id FROM steps s JOIN runs r ON s.run_id=r.id"
        "  WHERE substr(r.created_at,1,7)=?)", (ym,)
    ).fetchone()[0]
    return {"runs": runs, "steps": steps, "messages": messages}


class TraceMonthlyMigrator:
    """按月拆分迁移器：备份 → 逐月复制 → 校验 → 主库瘦身。"""

    def __init__(self, sources: list[str]):
        # sources[0] 为规范主库（迁移后瘦身保留 reviews）；其余为 CWD 残留旧库（迁移后移除）
        self.sources = [str(Path(s)) for s in sources if Path(s).exists()]

    # ── 预览 ──────────────────────────────────────────────────────
    def plan(self) -> dict:
        months: dict[str, int] = {}
        for src in self.sources:
            conn = _open_ro(src)
            try:
                if not _has_trace_tables(conn):
                    print(f"{TAG} 跳过（无 runs 表）：{src}")
                    continue
                for ym, n in _months_of(conn):
                    months[ym] = months.get(ym, 0) + n
            finally:
                conn.close()
        return {"sources": self.sources, "months": dict(sorted(months.items()))}

    # ── 迁移 ──────────────────────────────────────────────────────
    def migrate(self) -> dict:
        plan = self.plan()
        result = {"plan": plan, "copied": {}, "skipped": {}}
        if not plan["months"]:
            print(f"{TAG} 无可迁移数据，退出")
            return result

        print(f"{TAG} 源库：{plan['sources']}")
        for ym, n in plan["months"].items():
            print(f"{TAG}   {ym}: {n} runs → {_month_db_path(ym)}")

        backups = self._backup_all()
        copied: dict[str, int] = {}
        skipped: dict[str, int] = {}

        for src in self.sources:
            n = self._migrate_source(src, copied, skipped)
            if n:
                print(f"{TAG} {src} 迁移完成（{n} 个月份）")

        self._verify(copied, skipped)
        self._finalize(backups)
        print(f"{TAG} 迁移完成：copied={copied} skipped={skipped}")
        result["copied"] = copied
        result["skipped"] = skipped
        return result

    def _backup_all(self) -> list[str]:
        backups = []
        for src in self.sources:
            _wal_checkpoint_and_warn(src)
            bak = f"{src}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(src, bak)
            print(f"{TAG} 已备份 {src} → {bak}")
            backups.append(bak)
        return backups

    def _migrate_source(self, src: str, copied: dict, skipped: dict) -> int:
        """单源库迁移：只读打开源库，逐月 ATTACH 目标月库批量复制。返回处理的月份数。"""
        work = _open_ro(src)
        try:
            if not _has_trace_tables(work):
                return 0
            months = [ym for ym, _ in _months_of(work)]
            for idx, ym in enumerate(months):
                self._migrate_month(work, ym, f"m{idx}", copied, skipped)
            return len(months)
        finally:
            work.close()

    def _migrate_month(self, work: sqlite3.Connection, ym: str,
                       alias: str, copied: dict, skipped: dict) -> None:
        from utils.agent_trace.models import _ensure_db
        month_path = _month_db_path(ym)
        # 目标月库不存在时先建好三表（ATTACH 不建表）；目录可能尚未创建
        Path(month_path).parent.mkdir(parents=True, exist_ok=True)
        _ensure_db(Path(month_path), full=False)

        work.execute(f"ATTACH DATABASE ? AS {alias}", (month_path,))
        try:
            ins_runs = work.execute(
                f"INSERT OR IGNORE INTO {alias}.runs(id,agent_name,input,output,status,error,"
                f"created_at,finished_at,duration_ms) "
                f"SELECT id,agent_name,input,output,status,error,created_at,finished_at,duration_ms "
                f"FROM main.runs WHERE substr(created_at,1,7)=?", (ym,)).rowcount
            ins_steps = work.execute(
                f"INSERT OR IGNORE INTO {alias}.steps(id,run_id,event_run_id,parent_run_id,"
                f"step_type,step_name,input,output,status,error,started_at,finished_at,"
                f"duration_ms,extra) "
                f"SELECT id,run_id,event_run_id,parent_run_id,step_type,step_name,input,output,"
                f"status,error,started_at,finished_at,duration_ms,extra "
                f"FROM main.steps WHERE run_id IN "
                f"(SELECT id FROM main.runs WHERE substr(created_at,1,7)=?)", (ym,)).rowcount
            ins_msgs = work.execute(
                f"INSERT OR IGNORE INTO {alias}.messages(id,step_id,seq,role,content,"
                f"tool_calls,tool_call_id,reasoning) "
                f"SELECT g.id,g.step_id,g.seq,g.role,g.content,g.tool_calls,g.tool_call_id,g.reasoning "
                f"FROM main.messages g "
                f"JOIN main.steps s ON g.step_id=s.id "
                f"JOIN main.runs r ON s.run_id=r.id "
                f"WHERE substr(r.created_at,1,7)=?", (ym,)).rowcount
            work.commit()
            ins_total = ins_runs + ins_steps + ins_msgs
            # skipped = 源行数合计 - 本次新插入（重跑时 inserted=0，即整月跳过）
            src_total = sum(_source_counts(work, ym).values())
            copied[ym] = copied.get(ym, 0) + ins_total
            skipped[ym] = skipped.get(ym, 0) + max(0, src_total - ins_total)
            after = _month_counts(work, alias)
            print(f"{TAG}   {ym}: runs+{ins_runs} steps+{ins_steps} messages+{ins_msgs} "
                  f"→ 月库累计 {after}")
        finally:
            work.execute(f"DETACH DATABASE {alias}")

    # ── 校验与收尾 ────────────────────────────────────────────────
    def _verify(self, copied: dict, skipped: dict) -> None:
        """终态校验：每个源库三表行数（JOIN 口径）== 对应各月库行数之和。"""
        for src in self.sources:
            conn = _open_ro(src)
            try:
                if not _has_trace_tables(conn):
                    continue
                for ym, _ in _months_of(conn):
                    src_counts = _source_counts(conn, ym)
                    month = _open_ro(_month_db_path(ym))
                    try:
                        dst = {
                            "runs": month.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                            "steps": month.execute("SELECT COUNT(*) FROM steps").fetchone()[0],
                            "messages": month.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                        }
                    finally:
                        month.close()
                    for t in ("runs", "steps", "messages"):
                        if dst[t] < src_counts[t]:
                            raise RuntimeError(
                                f"{TAG} 校验失败：{ym} {t} 月库 {dst[t]} < 源 {src_counts[t]}，"
                                f"请检查后重跑（幂等续传）")
            finally:
                conn.close()
        print(f"{TAG} 校验通过：各月库行数 >= 源库行数（含历史重跑已迁移部分）")

    def _finalize(self, backups: list[str]) -> None:
        """主库瘦身（DROP 三表 + VACUUM）；CWD 残留旧库移除（备份副本已存在）。"""
        main_db = str(Path(get_db_dir()) / "agent_trace.db")
        if main_db in self.sources and Path(main_db).exists():
            conn = sqlite3.connect(main_db)
            try:
                for t in ("messages", "steps", "runs"):  # 先子后父，避开 FK
                    conn.execute(f"DROP TABLE IF EXISTS {t}")
                conn.commit()
                conn.execute("VACUUM")
            finally:
                conn.close()
            size = Path(main_db).stat().st_size / 1024 / 1024
            print(f"{TAG} 主库已瘦身（仅 reviews）：{main_db}（{size:.2f} MB）")
        # CWD 残留旧库：迁移完即移除（其 .bak 副本已在 backups）
        for src in self.sources:
            if Path(src).resolve() != Path(main_db).resolve() and Path(src).exists():
                Path(src).unlink()
                print(f"{TAG} 已移除 CWD 残留旧库 {src}（备份：{backups}）")


def _has_trace_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('runs','steps','messages')"
    ).fetchone()
    return row[0] == 3


def _month_counts(work: sqlite3.Connection, alias: str) -> dict:
    return {
        "runs": work.execute(f"SELECT COUNT(*) FROM {alias}.runs").fetchone()[0],
        "steps": work.execute(f"SELECT COUNT(*) FROM {alias}.steps").fetchone()[0],
        "messages": work.execute(f"SELECT COUNT(*) FROM {alias}.messages").fetchone()[0],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent_trace.db 按月分库存量迁移")
    parser.add_argument("--apply", action="store_true", help="正式执行（默认 dry-run 预览）")
    args = parser.parse_args(argv)

    sources = [str(Path(get_db_dir()) / "agent_trace.db"), "agent_trace.db"]  # 规范主库 + CWD 残留
    mig = TraceMonthlyMigrator(sources)
    plan = mig.plan()
    if not plan["months"]:
        print(f"{TAG} 无可迁移数据（sources={plan['sources']}）")
        return 0
    print(f"{TAG} 月份分布：{plan['months']}")
    if not args.apply:
        print(f"{TAG} dry-run 预览结束（未落盘）。确认后加 --apply 正式执行。")
        return 0
    mig.migrate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
