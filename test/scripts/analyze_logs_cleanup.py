"""日志清理分析脚本

扫描 logs 目录，统计并标记可清理日志：
- 旧日志：超过保留期（默认 14 天）
- 无关联日志：对话级日志的 uuid 已不在 stock_radar.db 中（对话已删除/孤立）

清理规则（核心：保留期内一律不删）：
- 保留期内（< keep_days）：一律保留，即使无关联
- 超过保留期的对话日志：无关联 → 可清理；有关联 → 默认保留（--force-old 可强制删）
- 超过保留期的 web / 未识别日志 → 可清理
- 系统日志（radar-system.log）：不删文件，按行时间戳清理旧内容（保留近 keep_days）
- 清理完日志后自动删除空的子目录

默认 dry-run 仅分析，加 --execute 实际删除。只删 .log 文件和空目录。

用法：
  python test/scripts/analyze_logs_cleanup.py                       # 分析报告（dry-run）
  python test/scripts/analyze_logs_cleanup.py --keep-days 7          # 自定义保留期
  python test/scripts/analyze_logs_cleanup.py --execute              # 执行删除
  python test/scripts/analyze_logs_cleanup.py --execute --force-old  # 连「旧但活跃」一起删
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# 对话日志: 2026-07-09-19-55-{uuid}.log
DIALOG_LOG_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.log$"
)
# web 日志: 2026-06-05.log
WEB_LOG_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.log$")
SYSTEM_LOG_NAME = "radar-system.log"
# 系统日志行首时间戳: 2026-08-07 22:15:12.012 │ ...
SYS_LOG_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass
class LogEntry:
    """单条日志文件的元信息与清理判定。"""

    path: Path
    name: str
    kind: str = "other"           # dialog | web | system | other
    uuid: str = ""
    date: str = ""                # YYYY-MM-DD（来自文件名）
    mtime: datetime = field(default_factory=datetime.now)
    size: int = 0
    related: bool = False         # 对话是否仍活跃（仅 dialog 有意义）
    deletable: bool = False
    reason: str = ""

    @property
    def size_kb(self) -> float:
        return self.size / 1024


@dataclass
class SysLogStat:
    """系统日志按行清理的统计。"""

    path: Path
    removable_lines: int = 0   # 可删行数（过期块）
    kept_lines: int = 0        # 保留行数


class LogCleaner:
    """日志清理分析器：扫描、分类、标记可清理日志，可选执行删除。"""

    def __init__(
        self,
        logs_dir: str,
        db_path: str,
        keep_days: int = 14,
        force_old: bool = False,
    ):
        self.logs_dir = Path(logs_dir)
        self.db_path = Path(db_path)
        self.keep_days = keep_days
        self.force_old = force_old
        self.cutoff = datetime.now() - timedelta(days=keep_days)
        self.cutoff_date = self.cutoff.strftime("%Y-%m-%d")
        self._active_uuids: set[str] | None = None

    # ── 扫描与分类 ──

    def scan(self) -> list[LogEntry]:
        """递归扫描 logs 目录下所有 .log 文件并完成判定。"""
        entries: list[LogEntry] = []
        if not self.logs_dir.exists():
            return entries
        for path in sorted(self.logs_dir.rglob("*.log")):
            entry = self._classify(path)
            self._evaluate(entry)
            entries.append(entry)
        return entries

    def _classify(self, path: Path) -> LogEntry:
        name = path.name
        st = path.stat()
        entry = LogEntry(
            path=path,
            name=name,
            mtime=datetime.fromtimestamp(st.st_mtime),
            size=st.st_size,
        )

        m = DIALOG_LOG_RE.match(name)
        if m:
            entry.kind = "dialog"
            entry.date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            entry.uuid = m.group(4)
            return entry

        m = WEB_LOG_RE.match(name)
        if m:
            entry.kind = "web"
            entry.date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            return entry

        if name == SYSTEM_LOG_NAME:
            entry.kind = "system"
            return entry

        entry.kind = "other"
        return entry

    # ── 活跃对话 uuid ──

    def active_uuids(self) -> set[str]:
        """从 stock_radar.db 加载仍存在对话记录的 uuid 集合。"""
        if self._active_uuids is not None:
            return self._active_uuids
        uuids: set[str] = set()
        if not self.db_path.exists():
            self._active_uuids = uuids
            return uuids
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                for table in ("web_dialogs", "web_messages"):
                    try:
                        rows = conn.execute(
                            f"SELECT DISTINCT dialog_uuid FROM {table}"
                        ).fetchall()
                        uuids.update(r[0] for r in rows if r[0])
                    except sqlite3.OperationalError:
                        continue  # 表不存在则跳过
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"[warn] 读取数据库失败: {e}", file=sys.stderr)
        self._active_uuids = uuids
        return uuids

    # ── 清理判定 ──

    def _evaluate(self, entry: LogEntry) -> None:
        is_old = entry.mtime < self.cutoff

        if entry.kind == "dialog":
            related = entry.uuid in self.active_uuids()
            entry.related = related
            # 保留期内一律不删（即使无关联，用户可能近期还要查）
            if not is_old:
                entry.reason = "保留期内" + ("" if related else "（无关联但近期）")
                return
            if not related:
                entry.deletable = True
                entry.reason = f"旧且无关联（超过 {self.keep_days} 天，对话已删）"
            elif self.force_old:
                entry.deletable = True
                entry.reason = "旧日志（--force-old 强制清理）"
            else:
                entry.reason = "旧但对话仍活跃（建议保留，--force-old 可删）"
            return

        if entry.kind == "web":
            if is_old:
                entry.deletable = True
                entry.reason = f"web 日志超过 {self.keep_days} 天"
            else:
                entry.reason = "web 日志（保留期内）"
            return

        if entry.kind == "system":
            # 文件本身不删，按行清理旧内容（见 analyze_system_log / clean_system_log）
            entry.reason = "系统日志（按行清理旧内容）"
            return

        # other：按保留期清理
        if is_old:
            entry.deletable = True
            entry.reason = f"未识别日志超过 {self.keep_days} 天"
        else:
            entry.reason = "未识别日志"

    # ── 系统日志按行清理 ──

    def _iter_system_blocks(self, path: Path) -> list[tuple[str, list[str]]]:
        """按行首时间戳把系统日志切成块，返回 [(date, [lines]), ...]。

        无时间戳的行归入当前块（通常是多行日志的续行）。
        """
        blocks: list[tuple[str, list[str]]] = []
        current_date = "9999-99-99"  # 文件开头无时间戳的行归为"最新"块，保守保留
        current_lines: list[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = SYS_LOG_TS_RE.match(line)
                    if m:
                        if current_lines:
                            blocks.append((current_date, current_lines))
                        current_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                        current_lines = [line]
                    else:
                        current_lines.append(line)
            if current_lines:
                blocks.append((current_date, current_lines))
        except OSError as e:
            print(f"[warn] 读取系统日志失败 {path}: {e}", file=sys.stderr)
        return blocks

    def analyze_system_log(self, path: Path) -> SysLogStat:
        """分析系统日志，返回可删/保留行数（不修改文件）。"""
        blocks = self._iter_system_blocks(path)
        removable = sum(len(ls) for d, ls in blocks if d < self.cutoff_date)
        kept = sum(len(ls) for d, ls in blocks if d >= self.cutoff_date)
        return SysLogStat(path=path, removable_lines=removable, kept_lines=kept)

    def clean_system_log(self, path: Path) -> tuple[int, int]:
        """实际清理系统日志旧内容，返回 (已删行数, 保留行数)。"""
        blocks = self._iter_system_blocks(path)
        kept_blocks = [(d, ls) for d, ls in blocks if d >= self.cutoff_date]
        removed = sum(len(ls) for d, ls in blocks if d < self.cutoff_date)
        kept = sum(len(ls) for _, ls in kept_blocks)
        if removed > 0:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for _, ls in kept_blocks:
                        f.writelines(ls)
            except OSError as e:
                print(f"[warn] 写入系统日志失败 {path}: {e}", file=sys.stderr)
                return 0, kept + removed
        return removed, kept

    # ── 空目录清理 ──

    def list_empty_dirs(self) -> list[Path]:
        """返回 logs 下所有空子目录（不含 logs 本身）。"""
        empties: list[Path] = []
        for path in self.logs_dir.rglob("*"):
            if path.is_dir() and not any(path.iterdir()):
                empties.append(path)
        return empties

    def clean_empty_dirs(self) -> int:
        """循环删除空子目录（删完子目录后父目录可能变空），返回删除数。"""
        removed = 0
        while True:
            emptied = 0
            for path in self.logs_dir.rglob("*"):
                if path.is_dir() and not any(path.iterdir()):
                    try:
                        path.rmdir()
                        removed += 1
                        emptied += 1
                    except OSError:
                        pass
            if emptied == 0:
                break
        return removed

    # ── 执行删除 ──

    @staticmethod
    def execute(entries: list[LogEntry]) -> tuple[int, int]:
        """删除标记为 deletable 的日志文件，返回 (删除数, 释放字节数)。"""
        deleted = 0
        freed = 0
        for e in entries:
            if not e.deletable:
                continue
            try:
                freed += e.path.stat().st_size
                e.path.unlink()
                deleted += 1
            except OSError as ex:
                print(f"[warn] 删除失败 {e.path}: {ex}", file=sys.stderr)
        return deleted, freed


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def format_report(
    entries: list[LogEntry],
    keep_days: int,
    dry_run: bool,
    sys_stat: SysLogStat | None,
    empty_dirs: list[Path],
) -> str:
    """格式化分析报告。"""
    lines: list[str] = []
    total = len(entries)
    deletable = [e for e in entries if e.deletable]
    freed_bytes = sum(e.size for e in deletable)

    lines.append("=" * 64)
    lines.append("日志清理分析报告" + ("（dry-run，未实际删除）" if dry_run else "（已执行删除）"))
    lines.append("=" * 64)
    lines.append(f"  保留期:      {keep_days} 天（cutoff: {datetime.now() - timedelta(days=keep_days):%Y-%m-%d}）")
    lines.append(f"  扫描日志:    {total} 个")
    lines.append(f"  可清理文件:  {len(deletable)} 个（{_fmt_size(freed_bytes)}）")
    if sys_stat:
        lines.append(
            f"  系统日志:    可删 {sys_stat.removable_lines} 行 / 保留 {sys_stat.kept_lines} 行"
            f"（{sys_stat.path.name}）"
        )
    if empty_dirs:
        lines.append(f"  空目录:      {len(empty_dirs)} 个")

    # 按类别统计
    by_kind: dict[str, list[LogEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)

    lines.append("")
    lines.append("── 按类别统计 ──")
    lines.append(f"  {'类别':10s} {'总数':>5s} {'可清理':>6s} {'大小':>10s}")
    for kind in ("dialog", "web", "system", "other"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        d = [e for e in items if e.deletable]
        sz = sum(e.size for e in items)
        lines.append(f"  {kind:10s} {len(items):>5d} {len(d):>6d} {_fmt_size(sz):>10s}")

    # 可清理清单（按时间倒序，旧的在前）
    if deletable:
        lines.append("")
        lines.append(f"── 可清理文件清单（{len(deletable)} 个）──")
        for e in sorted(deletable, key=lambda x: x.mtime)[:50]:
            lines.append(
                f"  {e.mtime:%Y-%m-%d}  {_fmt_size(e.size):>9s}  "
                f"{e.reason:36s}  {e.name}"
            )
        if len(deletable) > 50:
            lines.append(f"  ... 还有 {len(deletable) - 50} 个未显示")

    # 旧但活跃的对话日志（需人工确认）
    old_active = [
        e for e in entries
        if e.kind == "dialog" and e.related and not e.deletable and "旧但" in e.reason
    ]
    if old_active:
        lines.append("")
        lines.append(f"── 旧但对话仍活跃（{len(old_active)} 个，建议保留，--force-old 可删）──")
        for e in sorted(old_active, key=lambda x: x.mtime)[:20]:
            lines.append(f"  {e.mtime:%Y-%m-%d}  {e.uuid[:8]}  {e.name}")

    # 空目录清单
    if empty_dirs:
        lines.append("")
        lines.append(f"── 空目录（{len(empty_dirs)} 个，将删除）──")
        for p in empty_dirs[:20]:
            try:
                lines.append(f"  {p.relative_to(p.parents[-1])}")
            except ValueError:
                lines.append(f"  {p}")

    lines.append("")
    return "\n".join(lines)


def _resolve_default_paths() -> tuple[str, str]:
    """优先用 utils.app_paths 解析路径，失败则回退相对路径。"""
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    try:
        from utils.app_paths import get_db_path, get_logs_dir
        return get_logs_dir(), get_db_path()
    except Exception:
        return str(project_root / "logs"), str(project_root / "stock_radar.db")


def main() -> int:
    default_logs, default_db = _resolve_default_paths()
    parser = argparse.ArgumentParser(description="日志清理分析：标记并可选删除旧/无关联日志")
    parser.add_argument("--keep-days", type=int, default=14, help="保留天数（默认 14）")
    parser.add_argument("--execute", action="store_true", help="实际执行删除（默认 dry-run）")
    parser.add_argument("--force-old", action="store_true",
                        help="强制清理「旧但对话仍活跃」的日志（默认保留）")
    parser.add_argument("--logs-dir", default=default_logs, help="日志目录")
    parser.add_argument("--db-path", default=default_db, help="stock_radar.db 路径")
    args = parser.parse_args()

    cleaner = LogCleaner(
        logs_dir=args.logs_dir,
        db_path=args.db_path,
        keep_days=args.keep_days,
        force_old=args.force_old,
    )
    entries = cleaner.scan()

    # 系统日志分析 + 空目录扫描（dry-run 也展示）
    sys_entry = next((e for e in entries if e.kind == "system"), None)
    sys_stat = cleaner.analyze_system_log(sys_entry.path) if sys_entry else None
    empty_dirs = cleaner.list_empty_dirs()

    print(format_report(entries, args.keep_days, dry_run=not args.execute,
                        sys_stat=sys_stat, empty_dirs=empty_dirs))

    if args.execute:
        deleted, freed = cleaner.execute(entries)
        msg_parts = [f"删除 {deleted} 个日志文件（{_fmt_size(freed)}）"]
        if sys_stat and sys_stat.removable_lines > 0:
            rm, kept = cleaner.clean_system_log(sys_stat.path)
            msg_parts.append(f"清理系统日志 {rm} 行（保留 {kept} 行）")
        if empty_dirs:
            rm_dirs = cleaner.clean_empty_dirs()
            msg_parts.append(f"删除 {rm_dirs} 个空目录")
        print("\n✅ " + "，".join(msg_parts))
    elif any(e.deletable for e in entries) or (sys_stat and sys_stat.removable_lines) or empty_dirs:
        print("\n💡 这是 dry-run。确认后加 --execute 实际删除。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
