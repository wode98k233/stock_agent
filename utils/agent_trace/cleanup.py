"""agent_trace 月库清理：保留最近 N 个月，过期月库直接删文件（preview/apply 两段式）。

用法（经 utils.agent_trace CLI）：
    python -m utils.agent_trace cleanup                # 预览（默认保留 6 个月）
    python -m utils.agent_trace cleanup --apply        # 正式删除
    python -m utils.agent_trace cleanup --keep-months 3 --apply

删除语义：月库文件（db/-wal/-shm）整体移除，即"清理=删文件"；
主库 reviews 中指向被删 run 的记录级联清理（scope='run'）。
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path

from utils.agent_trace.store import store

logger = logging.getLogger(__name__)

# 默认保留月数（模块常量；签名留参便于日后接配置）
TRACE_KEEP_MONTHS = 6

_SIDECAR_SUFFIXES = ("", "-wal", "-shm")


@dataclass
class MonthPlan:
    """单个月库的清理计划。"""
    month: str
    db_path: str
    size_bytes: int
    runs: int
    steps: int
    messages: int
    reviews_to_delete: int = 0

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / 1024 / 1024, 2)


@dataclass
class CleanupPlan:
    """一次清理的完整计划（preview 结果，apply 输入）。"""
    keep_months: int
    months: list[MonthPlan] = field(default_factory=list)

    @property
    def total_size_mb(self) -> float:
        return round(sum(m.size_bytes for m in self.months) / 1024 / 1024, 2)

    @property
    def total_runs(self) -> int:
        return sum(m.runs for m in self.months)


def _month_index(ym: str) -> int:
    """YYYYMM → 月序号（用于自然月加减）。"""
    return int(ym[:4]) * 12 + int(ym[4:])


def preview(keep_months: int = TRACE_KEEP_MONTHS) -> CleanupPlan:
    """统计将清理的过期月库（不动任何文件）。

    语义：以当前月为锚保留最近 keep_months 个**自然月**（keep_months=0 全清）。
    """
    current = store.current_month()
    cutoff = _month_index(current) - (keep_months - 1) if keep_months > 0 else 10 ** 9
    expired = [ym for ym in store.iter_existing_months()
               if _month_index(ym) < cutoff]
    plan = CleanupPlan(keep_months=keep_months)
    for ym in expired:
        db_path = store.month_db_path(ym)
        size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
        conn = store.conn_for_month(ym)
        if conn is None:
            continue
        try:
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            steps = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            store.forget_month(ym)
        plan.months.append(MonthPlan(
            month=ym, db_path=db_path, size_bytes=size,
            runs=runs, steps=steps, messages=messages))
    return plan


def _cascade_delete_reviews(plan: CleanupPlan) -> int:
    """主库级联清理被删 run 的评审记录。"""
    total = 0
    for m in plan.months:
        run_ids = store.run_ids_in_month(m.month)
        if run_ids:
            total += store.delete_reviews_for_runs(run_ids)
        m.reviews_to_delete = len(run_ids)
    return total


def _remove_month_files(ym: str) -> None:
    store.forget_month(ym)
    db_path = store.month_db_path(ym)
    for suffix in _SIDECAR_SUFFIXES:
        p = Path(db_path + suffix)
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            logger.warning("[trace-cleanup] 删除失败 %s: %s", p, e)


def apply(plan: CleanupPlan) -> dict:
    """执行清理：级联删主库 reviews → 删月库文件。返回统计。"""
    if not plan.months:
        return {"removed_months": [], "freed_mb": 0.0, "reviews_deleted": 0}

    reviews_deleted = _cascade_delete_reviews(plan)
    removed = []
    for m in plan.months:
        _remove_month_files(m.month)
        removed.append(m.month)
        logger.info("[trace-cleanup] 已删除月库 %s", m.db_path)
    return {
        "removed_months": removed,
        "freed_mb": plan.total_size_mb,
        "reviews_deleted": reviews_deleted,
    }


def cleanup(keep_months: int = TRACE_KEEP_MONTHS, do_apply: bool = False) -> dict:
    """preview + （可选）apply 一步走（编程入口）。"""
    plan = preview(keep_months)
    if not do_apply:
        return {"plan": plan, "result": None}
    return {"plan": plan, "result": apply(plan)}
