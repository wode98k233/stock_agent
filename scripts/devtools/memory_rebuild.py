#!/usr/bin/env python
"""记忆库重建 + 端到端回测（方案 A，2026-09-23）。

流程：
  1. 备份 `db/stock_memory.db` 与 `chroma_data/`（时间戳后缀）；
  2. 从旧库**筛选干净报告**（非 superseded、非失败残片、长度达标、按标的+日期去重）；
  3. 用**新 chunker** 重新分块，**批量** embedding（`gateway.embed(list)`，省免费额度）；
  4. 写入**全新** `stock_memory_rebuilt.db` + `chroma_rebuilt/`（FTS5 含 chunk 元数据 + Chroma）；
  5. 在新库跑端到端 `retrieve`（hybrid + rerank + 自适应门槛 + 新注入），评估自命中/注入量。

用法:
    python scripts/devtools/memory_rebuild.py --dry-run                 # 只备份+看筛选结果（零 API）
    python scripts/devtools/memory_rebuild.py --limit 20                # 构建 + 评估
"""
import argparse
import os
import shutil
import sys
import time
from datetime import datetime

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from memory_backtest import _load_reports, _group_reports  # noqa: E402


def _backup(src_db, chroma_dir, stamp):
    bak_dir = os.path.join(os.path.dirname(src_db), f"_backup_{stamp}")
    os.makedirs(bak_dir, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = src_db + suffix
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(bak_dir, os.path.basename(p)))
    if os.path.isdir(chroma_dir):
        shutil.copytree(chroma_dir, os.path.join(bak_dir, os.path.basename(chroma_dir)))
    return bak_dir


def _select_clean(groups, limit, dedupe=True):
    """筛选干净报告：跳过失败残片；可选按 (stock_code,date) 去重保留最新；取最近 limit 篇。"""
    from memory.sdk import _is_incomplete_result
    seen, out = set(), []
    for g in groups:
        if len(g["text"]) < 300:
            continue
        if _is_incomplete_result(g["text"]):
            continue
        if dedupe:
            dedup = (g["stock_code"], g["date"])
            if dedup in seen:
                continue
            seen.add(dedup)
        out.append(g)
        if len(out) >= limit:
            break
    return out


def _batch_embed(texts, batch, sleep):
    from utils.llm import gateway, Purpose
    cache = {}
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        vecs = gateway.embed(part, purpose=Purpose.MEMORY)
        for t, v in zip(part, vecs):
            cache[t] = v
        if i + batch < len(texts):
            time.sleep(sleep)
    return cache


def main():
    ap = argparse.ArgumentParser(description="记忆库重建 + 端到端回测")
    ap.add_argument("--limit", type=int, default=20, help="重建报告数")
    ap.add_argument("--batch", type=int, default=16, help="embedding 批大小")
    ap.add_argument("--sleep", type=float, default=1.0, help="批间 sleep 秒")
    ap.add_argument("--source-db", default=None)
    ap.add_argument("--target-db", default=None)
    ap.add_argument("--target-chroma", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只备份+筛选，不 embedding")
    ap.add_argument("--keep-dupes", action="store_true", help="不去重（保留同日同标的多份报告）")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（默认非 dry-run 时备份一次）")
    args = ap.parse_args()

    from utils.app_paths import get_stock_memory_db_path, get_chroma_data_path
    src_db = args.source_db or get_stock_memory_db_path()
    src_chroma = get_chroma_data_path()
    target_db = args.target_db or os.path.join(os.path.dirname(src_db), "stock_memory_rebuilt.db")
    target_chroma = args.target_chroma or os.path.join(os.path.dirname(os.path.dirname(src_db)), "chroma_rebuilt")

    print(f"源库: {src_db}\n源向量: {src_chroma}")
    print(f"目标库: {target_db}\n目标向量: {target_chroma}")

    rows = _load_reports(src_db)
    groups = _group_reports(rows)
    clean = _select_clean(groups, args.limit, dedupe=not args.keep_dupes)
    print(f"归组报告={len(groups)} → 干净样本={len(clean)}（limit={args.limit} dedupe={not args.keep_dupes}）")
    for g in clean[:8]:
        print(f"   - {g['date']} {g['stock_name']}({g['stock_code']}) "
              f"{g['source_query'][:30]!r} {len(g['text'])}字")
    if args.dry_run:
        print("[dry-run] 结束（未备份、未 embedding、未写库）")
        return

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = _backup(src_db, src_chroma, stamp)
        print(f"[备份] → {bak}")

    # 重建：新 chunker + 批量 embedding + 新库
    import uuid
    import memory.backend.embedding as emb_mod
    emb_mod.get_chroma_data_path = lambda: target_chroma  # 让 EmbeddingBackend 指向新向量目录

    from memory.chunking import chunk_report, strip_report_decor, dashboard_to_text, strip_chart_markers
    from memory.backend.base import MemoryEntry
    from memory.backend.fts5 import FTS5Backend, ensure_jieba_ready
    from memory.backend.embedding import EmbeddingBackend
    from memory.backend.hybrid import HybridBackend
    ensure_jieba_ready()

    # 重置目标
    for p in (target_db, target_db + "-wal", target_db + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(target_chroma):
        shutil.rmtree(target_chroma)

    entries, texts = [], []
    for g in clean:
        text = strip_chart_markers(dashboard_to_text(strip_report_decor(g["text"])))
        chunks = chunk_report(text)
        gid = uuid.uuid4().hex[:8]
        for i, c in enumerate(chunks):
            meta = {
                "date": g["date"], "source_query": g["source_query"],
                "subject_kind": g["subject_kind"], "tags": [],
                "chunk_index": i, "chunk_count": len(chunks), "chunk_group_id": gid,
            }
            entries.append(MemoryEntry(
                entry_id="", stock_code=g["stock_code"], stock_name=g["stock_name"],
                content=c, metadata=meta, confidence=float(g["confidence"] or 0.5)))
            texts.append(c)
    print(f"\n[重建] 报告={len(clean)} 片段={len(entries)} → 批量 embedding（batch={args.batch}）")
    t0 = time.time()
    cache = _batch_embed(texts, args.batch, args.sleep)
    print(f"[重建] embedding 完成 {time.time()-t0:.1f}s（{len(texts)} 片段）")

    def cached_fn(t):
        return cache.get(t) or _single_embed(t)

    fts5 = FTS5Backend(target_db)
    emb = EmbeddingBackend(embedding_fn=cached_fn, chroma_path=target_chroma, min_similarity=0.0)
    hb = HybridBackend(fts5, emb)
    hb.add_batch(entries)
    print(f"[重建] 新库写入完成：FTS5 count={fts5.count()} chroma count={emb.count()}")

    # ── 端到端评估（新库）──
    from memory.sdk import MemorySDK, MemoryConfig, _build_embedding_fn
    sdk = MemorySDK(MemoryConfig(
        backend_mode="auto", embedding_fn=_build_embedding_fn(),
        memory_db_path=target_db, retrieval_top_k=8,
        min_similarity=0.30, similarity_margin=0.12,
        max_prompt_chars=6000, max_chunks_per_group=6, chunk_max_chars=1000,
    ))
    print("\n[评估] 新库 retrieve（自命中 = 注入块含本报告的标的）")
    hit, total, lens = 0, 0, []
    samples = []
    for g in clean:
        q = (g["source_query"] or "").strip()
        if not q:
            continue
        total += 1
        block = sdk.retrieve(q)
        lens.append(len(block))
        ok = (g["stock_code"] and g["stock_code"] in block) or \
             (g["stock_name"] and g["stock_name"] in block) or \
             (g["subject_kind"] == "market" and "大盘" in block)
        if ok:
            hit += 1
        if len(samples) < 2 and block:
            samples.append((q, block[:500]))
    lens.sort()
    if total:
        print(f"  自命中率={hit/total*100:.0f}%（{hit}/{total}）  "
              f"注入长度 p50={lens[len(lens)//2]} max={lens[-1]}")
    for q, b in samples:
        print(f"\n--- 样例 query={q!r} ---\n{b}")


def _single_embed(t):
    from utils.llm import gateway, Purpose
    return gateway.embed([t], purpose=Purpose.MEMORY)[0]


if __name__ == "__main__":
    main()
