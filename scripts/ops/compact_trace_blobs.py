"""存量 trace 库瘦身回填：① 按 T1 规则重写历史行 ② 长文本内容寻址压缩（T2）。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md`` §7.3

为什么需要它
------------
T1/T2 只对新写入生效；存量月库仍是"整份 prompt/state 全量落盘"的老格式。本脚本把
**同一套规则**应用到历史行，所以不需要新读点：

- **阶段 A（digest）**：`steps.input` 里 `llm` 步改成 `llm_prompt_ref` 摘要（读侧由
  该步 `messages` 重建）、非 `tool` 步改成 `state_digest`；`steps.output` 同理。
  `tool` 步**一律不动**（入参出参是证据原文，9 个读点直接消费）。
- **阶段 B（blob）**：剩下仍 ≥1KB 的长文本（`messages.content/reasoning/tool_calls`、
  `steps.input/output`）按内容寻址压进 `blobs`，行内只留哨兵。
  `runs.input/output` **绝不压缩**（`store.search_runs` 用 SQL LIKE 搜它）。

用法（默认**预演**，不写任何数据）::

    python scripts/ops/compact_trace_blobs.py                 # 全部月库预演
    python scripts/ops/compact_trace_blobs.py --month 202610  # 单月预演
    python scripts/ops/compact_trace_blobs.py --apply         # 真回填（自动备份 + VACUUM）

⚠️ 四条纪律
-----------
1. **幂等**：已带 `_kind` 摘要的行跳过；已压缩的哨兵值跳过。可重复执行。
2. **先备份**：`--apply` 前用 SQLite 在线备份 API 复制成 `<db>.bak-<时间戳>`
   （⚠️ 不能 `copy2`：WAL 里未 checkpoint 的事务只在 `-wal`，硬拷贝会丢最后几笔）。
3. **VACUUM 要空间**：SQLite 删改不会自动回收文件页（`freelist` 会涨），收尾必须
   `VACUUM`；实测需约 1 倍文件大小的临时空间，不足时只提示不执行。
4. **必须停掉服务再跑**：正在运行的 `python -m server` / TUI 是**旧代码**，没有透明解压
   行工厂，压缩后它会读到哨兵串（`\x01Bzlib:...`）。脚本会先探锁，库被占用时直接拒绝。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.agent_trace import blobstore
from utils.agent_trace.payload import (
    LLM_PROMPT_KIND,
    STATE_KIND,
    digest_state,
    summary_kind,
)

# tool 步的 input/output 是证据原文，任何阶段都不动
_PROTECTED_STEP_TYPES = ("tool", "llm_error")
_BATCH = 200


def _fmt(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


def _file_bytes(db: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def _btext(text: str) -> int:
    """UTF-8 字节数（⚠️ len(str) 是字符数，中文占 3 字节，口径必须统一到字节）。"""
    return len(text.encode("utf-8", "replace"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _digest_llm_input(text: str, msg_count: int) -> str:
    """把历史 llm 步的 prompt 原文换成引用摘要。"""
    return json.dumps(
        {
            "_kind": LLM_PROMPT_KIND,
            "msg_count": msg_count,
            "chars": len(text),
            "sha256": _sha16(text),
            "preview": text[:512],
        },
        ensure_ascii=False,
        indent=2,
    )


def _digest_state_text(text: str) -> str | None:
    """把历史节点步的 state 原文换成有界摘要；解析失败返回 None（交给阶段 B）。"""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(data, dict):
        return json.dumps(digest_state(data), ensure_ascii=False, indent=2)
    # 非 dict（如 list）：保留结构元信息 + 片段，避免整块丢失
    return json.dumps(
        {
            "_kind": STATE_KIND,
            "_orig_chars": len(text),
            "_keys": [],
            "_note": "历史行：顶层为数组，仅保留元信息（内容见 tool 步与 messages）",
        },
        ensure_ascii=False,
        indent=2,
    )


def _plan_digest(conn: sqlite3.Connection) -> list[tuple]:
    """阶段 A 的更新计划：[(step_id, field, new_value_text, old_len, new_len)]。"""
    msgs_by_step: dict[int, int] = {}
    for step_id, count in conn.execute(
        "SELECT step_id, COUNT(*) FROM messages GROUP BY step_id"
    ):
        msgs_by_step[int(step_id)] = int(count)

    plan = []
    for step_id, step_type, input_text, output_text in conn.execute(
        "SELECT id, step_type, input, output FROM steps"
    ):
        st = step_type or ""
        if st in _PROTECTED_STEP_TYPES:
            continue
        for field, text in (("input", input_text), ("output", output_text)):
            if not isinstance(text, str) or _btext(text) < 1024:
                continue
            if summary_kind(text) or blobstore.is_ref(text):
                continue  # 已处理（幂等）
            if st == "llm" and field == "input":
                new = _digest_llm_input(text, msgs_by_step.get(int(step_id), 0))
            else:
                new = _digest_state_text(text)
                if new is None:
                    continue  # 非 JSON 长文本留给阶段 B 压缩
            new_bytes = _btext(new)
            old_bytes = _btext(text)
            if new_bytes < old_bytes:
                plan.append((int(step_id), field, new, old_bytes, new_bytes))
    return plan


def _plan_blob(conn: sqlite3.Connection,
               replacements: dict | None = None) -> tuple[list[tuple], int]:
    """阶段 B 的计划：[(table, id, column, old_bytes)] + 可压缩字节数。

    ``replacements`` 是阶段 A 已确定的新值（(table,id,column) → 文本）——
    必须按**阶段 A 之后**的值来规划，否则两个阶段的节省量会重复计算
    （实测原样相加会得出"省 409MB > 文件 309MB"的荒谬数字）。
    """
    plan = []
    saving = 0
    specs = (
        ("messages", "content"),
        ("messages", "reasoning"),
        ("messages", "tool_calls"),
        ("steps", "input"),
        ("steps", "output"),
    )
    replacements = replacements or {}
    for table, column in specs:
        for row_id, text in conn.execute(
            f"SELECT id, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL AND length(cast({column} AS blob)) >= ?",
            (blobstore.MIN_CHARS,),
        ):
            if blobstore.is_ref(text):
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            text = replacements.get((table, int(row_id), column), text)
            if _btext(text) < blobstore.MIN_CHARS:
                continue  # 阶段 A 已把它缩到阈值以下
            # 预估压缩后体积（真压缩一次，用于预演口径，不做实际写入）
            raw = _btext(text)
            packed = len(zlib.compress(text.encode("utf-8"), 6))
            if packed + 40 >= raw:
                continue  # 压不动就不压（避免"压缩后更大"）
            plan.append((table, int(row_id), column, raw))
            saving += raw - packed
    return plan, saving


def preview_db(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        digest_plan = _plan_digest(conn)
        replacements = {("steps", sid, field): new for sid, field, new, _ob, _nb in digest_plan}
        blob_plan, blob_saving = _plan_blob(conn, replacements)
        before = _file_bytes(db)
        # digest_plan tuple = (step_id, field, new_text, old_len, new_len)
        digest_saving = sum(p[3] for p in digest_plan) - sum(p[4] for p in digest_plan)
        return {
            "db": db.name,
            "before": before,
            "digest_rows": len(digest_plan),
            "digest_saving": digest_saving,
            "blob_rows": len(blob_plan),
            "blob_saving": blob_saving,
            "projected": max(before - digest_saving - blob_saving, 0),
            "_digest_plan": digest_plan,
            "_blob_plan": blob_plan,
        }
    finally:
        conn.close()


def _backup(db: Path) -> Path:
    """在线备份到 ``<db>.bak-<ts>``。

    ⚠️ **不能用 ``shutil.copy2``**：月库开的是 WAL，已提交但尚未 checkpoint 的事务
    只在 ``-wal`` 文件里，只拷主库文件会得到一个"少最后几笔"的备份（演练实测：
    4795 步的库拷出来只有 4790 步）。必须走 SQLite 的在线备份 API。
    """
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = Path(f"{db}.bak-{stamp}")
    # 同一秒内重复 apply（重试 / 连跑两次）会撞名，后一次覆盖前一次 ——
    # 被覆盖的正是"回填前"那份唯一能回滚的快照。撞名就加序号。
    dedup = 1
    while backup.exists():
        dedup += 1
        backup = Path(f"{db}.bak-{stamp}-{dedup}")
    src = sqlite3.connect(str(db))
    try:
        dst = sqlite3.connect(str(backup))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup


def _assert_not_locked(conn: sqlite3.Connection) -> None:
    """库被其他进程持写锁时立刻放弃（避免压缩到一半失败）。

    服务在跑时最典型的报错是 `database is locked`；提前用 BEGIN IMMEDIATE 探一次，
    比在批次中间炸掉更安全。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"[中止] 月库被占用（{exc}）。请先停掉正在运行的 python -m server / TUI —— "
            "旧进程没有透明解压，压缩后它会读到哨兵串；停服后再执行本脚本。"
        )


def _apply_db(db: Path, *, vacuum: bool = True, batch: int = _BATCH) -> dict:
    from utils.agent_trace.models import ensure_month_schema

    probe = sqlite3.connect(str(db), timeout=3)
    try:
        _assert_not_locked(probe)
    finally:
        probe.close()

    info = preview_db(db)
    digest_plan = info.pop("_digest_plan")
    blob_plan = info.pop("_blob_plan")
    backup = _backup(db)

    conn = sqlite3.connect(str(db))
    try:
        ensure_month_schema(conn)  # 补出 blobs 表（加性幂等）
        conn.commit()

        # 阶段 A：改写成摘要
        done = 0
        for step_id, field, new_text, _old, _new in digest_plan:
            conn.execute(f"UPDATE steps SET {field}=? WHERE id=?", (new_text, step_id))
            done += 1
            if done % batch == 0:
                conn.commit()
        conn.commit()

        # 阶段 B：长文本内容寻址压缩
        done = 0
        for table, row_id, column, _old_len in blob_plan:
            row = conn.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()
            if not row or not isinstance(row[0], str) or blobstore.is_ref(row[0]):
                continue  # 幂等：已被处理过
            ref = blobstore.put_blob(conn, row[0])
            conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (ref, row_id))
            done += 1
            if done % batch == 0:
                conn.commit()
        conn.commit()

        if vacuum:
            free = shutil.disk_usage(str(db.parent)).free
            need = _file_bytes(db) * 2
            if free < need:
                info["vacuum_skipped"] = f"磁盘不足（需约 {_fmt(need)}，剩 {_fmt(free)}）"
            else:
                try:
                    conn.execute("VACUUM")
                except sqlite3.OperationalError as exc:
                    # 数据已压缩，只是文件没缩；停服后重跑即可（脚本幂等）
                    info["vacuum_skipped"] = f"VACUUM 失败（{exc}）；数据已回填，停服后重跑可收尾"
    finally:
        conn.close()

    blobstore.clear_cache()
    info["backup"] = str(backup)
    info["after"] = _file_bytes(db)
    info["ratio"] = round(info["before"] / info["after"], 2) if info["after"] else 0.0
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="存量 trace 库瘦身回填（默认预演）")
    parser.add_argument("--month", default="", help="只处理某个月库，如 202610")
    parser.add_argument("--db", default="", help="直接指定月库文件路径（演练：可先复制一份再跑）")
    parser.add_argument("--apply", action="store_true", help="真回填（自动备份 + VACUUM）")
    parser.add_argument("--no-vacuum", action="store_true", help="不收尾 VACUUM（文件不会变小）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from utils.app_paths import get_trace_dir

    dbs = sorted(Path(get_trace_dir()).glob("agent_trace_??????.db"))
    if args.db:
        dbs = [Path(args.db)]
    elif args.month:
        dbs = [p for p in dbs if args.month in p.name]
    if not dbs:
        print("未找到月库")
        return 1

    total_before = total_projected = 0
    for db in dbs:
        if not args.apply:
            info = preview_db(db)
            _print(info, applied=False)
            total_before += info["before"]
            total_projected += info["projected"]
        else:
            info = _apply_db(db, vacuum=not args.no_vacuum)
            _print(info, applied=True)
            total_before += info["before"]
            total_projected += info["after"]

    print(f"\n合计：{_fmt(total_before)} → {_fmt(total_projected)}"
          f"（{total_before / max(total_projected, 1):.1f}x）"
          + ("" if args.apply else "　—— 预演，未写任何数据（--apply 才执行）"))
    return 0


def _print(info: dict, *, applied: bool) -> None:
    print(f"\n=== {info['db']} ===")
    print(f"当前 {_fmt(info['before'])}")
    print(f"  阶段A digest：{info['digest_rows']:5d} 行 → 省 {_fmt(info['digest_saving'])}")
    print(f"  阶段B blob  ：{info['blob_rows']:5d} 行 → 省 {_fmt(info['blob_saving'])}")
    if applied:
        print(f"  回填后 {_fmt(info['after'])}（{info['ratio']}x），备份 {Path(info['backup']).name}")
        if info.get("vacuum_skipped"):
            print(f"  ⚠️ 跳过 VACUUM：{info['vacuum_skipped']}")
    else:
        print(f"  预估回填后 {_fmt(info['projected'])}"
              f"（{info['before'] / max(info['projected'], 1):.1f}x）")


if __name__ == "__main__":
    raise SystemExit(main())
