# -*- coding: utf-8 -*-
"""修复存量记忆的 confidence 误判，并把 Chroma 向量库与 SQLite 对齐。

背景（2026-09-20）：`memory/metadata.py::collect_metadata` 从不回传 `_tool_extracts`
→ `sdk.archive` 永远取到空 → `infer_source` 判 `llm_speculation`（base=0.3）→
**所有**新记忆的 confidence 都被写成 0.3，被 `confidence_threshold=0.4` 整批挡在
检索之外（`semantic_memory_meta` 里 0.3 有 146 条、0.5 只有 71 条）。

两类修复：
1. **提权**：`confidence=0.3` → `0.5`（`report_fallback_repaired`，带 `repaired_from`
   留痕，可审计）。排除 `provenance.incomplete=true` 的失败残片——它们低是**有意的**。
2. **对齐**：confidence 同时写进了 **Chroma 向量库 metadata**（embedding 后端检索
   用的是它）。只改 SQLite 会让混合检索的 embedding 路径返回旧值 → 本脚本以
   SQLite（权威源）为准，把 Chroma 中不一致的条目刷齐。

用法：
    python scripts/ops/repair_memory_confidence.py             # dry-run
    python scripts/ops/repair_memory_confidence.py --apply     # 先备份再写库
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils.app_paths import get_stock_memory_db_path  # noqa: E402

OLD_CONF = 0.3
NEW_CONF = 0.5
REPAIRED_SOURCE = "report_fallback_repaired"


def _is_polluted_name(name: str) -> bool:
    """存量 stock_name 是否为指标词污染（如「市盈率T」）或为空。"""
    if not name:
        return True
    from memory.metadata import _looks_like_stock_name

    return not _looks_like_stock_name(str(name).strip())


def _backup(db_path: str) -> str:
    base, ext = os.path.splitext(db_path)
    dst = base + "_backup_pre_confrepair" + ext
    shutil.copy2(db_path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, dst + suffix)
    return dst


def _open_embedding_backend():
    """拿混合后端里的 EmbeddingBackend（Chroma 侧 confidence 同步用）。"""
    try:
        from memory import get_sdk

        be = get_sdk().backend
        return getattr(be, "_embedding", None)
    except Exception as exc:  # sdk 初始化失败不阻塞 dry-run
        print(f"!! 取 EmbeddingBackend 失败: {exc!r}")
        return None


def _load_sqlite(db_path: str):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT entry_id, confidence, provenance, confidence_factors, source_query "
        "FROM semantic_memory_meta"
    ).fetchall()
    conn.close()

    authority = {}       # entry_id -> SQLite 权威 confidence
    repair = []          # (entry_id, source_query, factors) 需提权的
    skipped_incomplete = 0
    for r in rows:
        try:
            prov = json.loads(r["provenance"]) if r["provenance"] else {}
        except (TypeError, ValueError):
            prov = {}
        incomplete = isinstance(prov, dict) and prov.get("incomplete")
        authority[r["entry_id"]] = float(r["confidence"])
        if float(r["confidence"]) == OLD_CONF:
            if incomplete:
                skipped_incomplete += 1       # 失败残片：低分是有意的，不动
                continue
            try:
                factors = json.loads(r["confidence_factors"]) if r["confidence_factors"] else {}
            except (TypeError, ValueError):
                factors = {}
            repair.append((r["entry_id"], r["source_query"] or "", factors))
    return authority, repair, skipped_incomplete


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只 dry-run）")
    ap.add_argument("--skip-names", action="store_true",
                    help="跳过 stock_name 污染修复")
    args = ap.parse_args()

    db_path = get_stock_memory_db_path()
    print(f"DB = {db_path}  exists={os.path.exists(db_path)}")

    authority, repair, skipped = _load_sqlite(db_path)
    dist = {}
    for v in authority.values():
        dist[v] = dist.get(v, 0) + 1
    print(f"SQLite confidence 分布: {sorted(dist.items())}")
    print(f"待提权 {len(repair)} 条（另 {skipped} 条失败残片保持 0.3，不动）")
    for eid, q, _f in repair[:8]:
        print(f"    {eid}  q={q[:52]}")
    if len(repair) > 8:
        print(f"    ... 其余 {len(repair) - 8} 条")

    # Chroma 侧现状
    emb = _open_embedding_backend()
    chroma_mismatch = []
    if emb is not None and emb.is_available():
        try:
            data = emb._collection.get(ids=list(authority), include=["metadatas"])
            for eid, meta in zip(data.get("ids", []) or [], data.get("metadatas", []) or []):
                if not isinstance(meta, dict):
                    continue
                ch = float(meta.get("confidence", 0.5) or 0.5)
                if abs(ch - authority.get(eid, 0.5)) > 1e-6:
                    chroma_mismatch.append((eid, ch, authority[eid]))
        except Exception as exc:
            print(f"!! 读取 Chroma 失败: {exc!r}")
        print(f"Chroma 与 SQLite 不一致 {len(chroma_mismatch)} 条")
        for eid, ch, au in chroma_mismatch[:8]:
            print(f"    {eid}  chroma={ch} → sqlite={au}")
    else:
        print("Chroma 不可用，跳过对齐（仅修 SQLite）。")

    if not repair and not chroma_mismatch:
        print("\nconfidence 无需修复。")
        if args.skip_names:
            return 0
        return fix_names(args.apply)

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行（会先自动备份）。")
        return 0

    backup = _backup(db_path)
    print(f"\n已备份 → {backup}")

    # 1) SQLite 提权
    conn = sqlite3.connect(db_path, timeout=15)
    cur = conn.cursor()
    changed = 0
    for eid, _q, factors in repair:
        factors = dict(factors or {})
        factors.setdefault("repaired_from", OLD_CONF)
        factors["source"] = REPAIRED_SOURCE
        cur.execute(
            "UPDATE semantic_memory_meta SET confidence = ?, confidence_factors = ? "
            "WHERE entry_id = ? AND confidence = ?",
            (NEW_CONF, json.dumps(factors, ensure_ascii=False), eid, OLD_CONF),
        )
        changed += cur.rowcount or 0
        authority[eid] = NEW_CONF
    conn.commit()
    conn.close()
    print(f"SQLite 已更新 {changed} 条。")

    # 2) Chroma 对齐（以修复后的 SQLite 为准）
    if emb is not None and chroma_mismatch:
        try:
            conf_map = {eid: authority[eid] for eid, _c, _a in chroma_mismatch}
            n = emb.update_confidence(conf_map)
            print(f"Chroma 已同步 {n} 条。")
        except Exception as exc:
            print(f"!! Chroma 同步失败（SQLite 已改，向量库仍旧值）: {exc!r}")
            print("   重跑本脚本 --apply 即可补齐。")

    after = sqlite3.connect(db_path)
    dist2 = after.execute(
        "SELECT confidence, COUNT(*) FROM semantic_memory_meta GROUP BY confidence ORDER BY 1"
    ).fetchall()
    after.close()
    print("修复后 SQLite confidence 分布:", [(c, n) for c, n in dist2])

    if not args.skip_names:
        return fix_names(args.apply)
    return 0


def fix_names(apply: bool) -> int:
    """修复存量 stock_name 的指标词污染（「市盈率T」），用 source_query 重提。

    同样需要 SQLite 与 Chroma 两侧一起改（展示层读哪边取决于检索路径）。
    """
    from memory.metadata import _extract_stock_name_from_input

    db_path = get_stock_memory_db_path()
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT entry_id, stock_code, stock_name, source_query FROM semantic_memory_meta"
    ).fetchall()

    updates = []  # (entry_id, new_name, old_name)
    for r in rows:
        old = (r["stock_name"] or "").strip()
        if not _is_polluted_name(old):
            continue
        new = _extract_stock_name_from_input({"query": r["source_query"] or ""})
        if not new and old:
            # 重提失败则清掉污染名（宁缺勿错；按 code 查询不受影响）
            updates.append((r["entry_id"], "", old))
        elif new and new != old:
            updates.append((r["entry_id"], new, old))

    print(f"\nstock_name 污染 {sum(1 for r in rows if _is_polluted_name((r['stock_name'] or '').strip()))} 条，"
          f"可修复 {len(updates)} 条")
    for eid, new, old in updates[:8]:
        print(f"    {eid}  {old!r} → {new!r}")
    if len(updates) > 8:
        print(f"    ... 其余 {len(updates) - 8} 条")
    conn.close()

    if not updates:
        return 0
    if not apply:
        print("[dry-run] 名字未写库。")
        return 0

    conn = sqlite3.connect(db_path, timeout=15)
    for eid, new, _old in updates:
        conn.execute("UPDATE semantic_memory_meta SET stock_name = ? WHERE entry_id = ?",
                     (new, eid))
    conn.commit()
    conn.close()
    print(f"SQLite 已修名字 {len(updates)} 条。")

    emb = _open_embedding_backend()
    if emb is not None and emb.is_available():
        try:
            name_map = {eid: new for eid, new, _o in updates if new}
            data = emb._collection.get(ids=list(name_map), include=["metadatas"])
            ids, metas = [], []
            for eid, meta in zip(data.get("ids", []) or [], data.get("metadatas", []) or []):
                if not isinstance(meta, dict) or eid not in name_map:
                    continue
                meta = dict(meta)
                meta["stock_name"] = name_map[eid]
                ids.append(eid)
                metas.append(meta)
            if ids:
                emb._collection.update(ids=ids, metadatas=metas)
                print(f"Chroma 已同步名字 {len(ids)} 条。")
        except Exception as exc:
            print(f"!! Chroma 名字同步失败: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
