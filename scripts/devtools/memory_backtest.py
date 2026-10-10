#!/usr/bin/env python
"""记忆回测 —— 分词 / 片段完整性 /（可选）相似度可靠性 + 相关召回。**只读源库，不写任何数据。**

关键前提：DB 里存的是**分块后的 chunk**（非完整报告）。故本脚本先按「报告」把 chunk 归组
（chunk_group_id 或 date+source_query），拼回完整报告再做后续分析。

设计（尊重免费 API 额度）：
- 离线部分零 API：分词覆盖、片段完整性（新 chunker）；
- 在线部分（`--with-embedding`）**批量** 调 embedding（`gateway.embed(list)`），分批 + 限流，
  跑「相似度分离」与「自召回 Recall@k / MRR」；
- 全程只读，不写任何 DB。

用法:
    python scripts/devtools/memory_backtest.py --limit 40                       # 离线
    python scripts/devtools/memory_backtest.py --limit 20 --with-embedding      # 在线（批量，省额度）
"""
import argparse
import math
import os
import sqlite3
import sys
import time

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load_reports(src_db: str):
    """只读读取「非 superseded 且正文足够长」的 chunk 行（含 rowid 用于组内排序）。"""
    conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT m.rowid AS _rid, m.entry_id, m.source_query, m.stock_code, m.stock_name, "
        "m.date, m.chunk_index, m.chunk_group_id, m.superseded_by, m.confidence, "
        "m.tags, m.subject_kind, m.sentiment, f.raw_content "
        "FROM semantic_memory_meta m "
        "LEFT JOIN semantic_memory_fts f ON f.entry_id = m.entry_id "
        "WHERE (m.superseded_by IS NULL OR m.superseded_by = '') "
        "AND f.raw_content IS NOT NULL AND LENGTH(f.raw_content) >= 1 "
        "ORDER BY m.date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _group_reports(rows):
    """把 chunk 行按报告归组并拼回完整报告。返回 [{key, source_query, date, text, n_chunks}]。"""
    groups = {}
    for r in rows:
        key = r.get("chunk_group_id") or f"{(r.get('date') or '')[:10]}|{r.get('source_query') or ''}"
        groups.setdefault(key, []).append(r)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda x: (x.get("chunk_index") or 0, x.get("_rid") or 0))
        text = "\n".join((it.get("raw_content") or "").strip() for it in items if it.get("raw_content"))
        if len(text) < 200:
            continue
        first = items[0]
        out.append({
            "key": key,
            "source_query": first.get("source_query") or "",
            "date": (first.get("date") or "")[:10],
            "stock_code": first.get("stock_code") or "",
            "stock_name": first.get("stock_name") or "",
            "subject_kind": first.get("subject_kind") or "",
            "tags": first.get("tags") or "[]",
            "sentiment": first.get("sentiment") or "",
            "confidence": first.get("confidence") or 0.5,
            "text": text,
            "n_chunks": len(items),
        })
    out.sort(key=lambda g: g["date"], reverse=True)
    return out


def phase_segment(groups):
    """离线：jieba 分词 + 查询↔报告（拼接后）的 token 覆盖率。"""
    from memory.backend.fts5 import FTS5Backend, ensure_jieba_ready
    ensure_jieba_ready()
    seg = FTS5Backend._segment

    covs, zero = [], 0
    for g in groups:
        q = (g["source_query"] or "").strip()
        if not q:
            continue
        qtok = {t for t in seg(q).split() if len(t) > 1}
        if not qtok:
            continue
        ctok = set(seg(g["text"]).split())
        hit = len(qtok & ctok) / len(qtok)
        covs.append(hit)
        if hit == 0:
            zero += 1
    covs.sort()
    n = len(covs)
    if not n:
        print("[分词] 无可用样本")
        return
    print(f"\n[分词] 报告数={n}")
    print(f"  query↔报告 token 覆盖率: min={covs[0]:.2f} p50={covs[n//2]:.2f} "
          f"p90={covs[int(n*0.9)]:.2f} max={covs[-1]:.2f}")
    print(f"  零覆盖（纯 FTS 必漏）占比: {zero/n*100:.0f}%")


def phase_chunk(groups):
    """离线：新 chunker 在完整报告上的片段完整性。"""
    from memory.chunking import chunk_report, strip_report_decor, \
        dashboard_to_text, strip_chart_markers

    sizes, tiny, n_report, n_chunk = [], 0, 0, 0
    for g in groups:
        text = strip_chart_markers(dashboard_to_text(strip_report_decor(g["text"])))
        chunks = chunk_report(text)
        n_report += 1
        n_chunk += len(chunks)
        for c in chunks:
            sizes.append(len(c))
            if len(c) < 120:
                tiny += 1
    sizes.sort()
    n = len(sizes)
    if not n:
        print("[片段] 无可用样本")
        return
    print(f"\n[片段完整性] 报告数={n_report} 片段数={n} 平均每报告={n_chunk/max(1,n_report):.1f} 片")
    print(f"  片段长度: min={sizes[0]} p10={sizes[int(n*0.1)]} p50={sizes[n//2]} "
          f"p90={sizes[int(n*0.9)]} max={sizes[-1]}")
    print(f"  <120 字碎渣占比: {tiny/n*100:.1f}%（合并前历史实测约 9%）")


def _embed_batch(texts, batch=16, sleep=1.0):
    from utils.llm import gateway, Purpose
    out = []
    for i in range(0, len(texts), batch):
        out.extend(gateway.embed(texts[i:i + batch], purpose=Purpose.MEMORY))
        if i + batch < len(texts):
            time.sleep(sleep)
    return out


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return s / (na * nb) if na and nb else 0.0


def phase_embedding(groups, limit, max_chunks, top_k):
    """在线：批量 embed → 相似度分离 + 自召回 Recall@k / MRR。"""
    from memory.chunking import chunk_report, strip_report_decor, \
        dashboard_to_text, strip_chart_markers

    sample = [g for g in groups if (g["source_query"] or "").strip()][:limit]
    docs, doc_owner, queries = [], [], []
    for ri, g in enumerate(sample):
        text = strip_chart_markers(dashboard_to_text(strip_report_decor(g["text"])))
        for c in chunk_report(text)[:3]:
            if len(docs) >= max_chunks:
                break
            docs.append(c[:480])
            doc_owner.append(ri)
        queries.append(g["source_query"].strip()[:200])
        if len(docs) >= max_chunks:
            break

    print(f"\n[在线] 待嵌入: {len(docs)} chunks + {len(queries)} queries（批量 16/批，省额度）")
    t0 = time.time()
    doc_vecs = _embed_batch(docs)
    q_vecs = _embed_batch(queries)
    print(f"[在线] 嵌入完成 {time.time()-t0:.1f}s")

    pos, neg = [], []
    for qi, qv in enumerate(q_vecs):
        for j in range(len(docs)):
            s = _cos(qv, doc_vecs[j])
            (pos if doc_owner[j] == qi else neg).append(s)
    if pos and neg:
        pos.sort(); neg.sort()
        print(f"  正例余弦 p50={pos[len(pos)//2]:.2f}(n={len(pos)}) | "
              f"负例余弦 p50={neg[len(neg)//2]:.2f}(n={len(neg)})")
        print(f"  正负均值差（分离度）: {(sum(pos)/len(pos)) - (sum(neg)/len(neg)):+.3f}")

    recall, rr, nq = 0, 0.0, 0
    for qi, qv in enumerate(q_vecs):
        own = {j for j in range(len(docs)) if doc_owner[j] == qi}
        if not own:
            continue
        nq += 1
        ranked = sorted(range(len(docs)), key=lambda j: _cos(qv, doc_vecs[j]), reverse=True)
        hit = next((rank for rank, j in enumerate(ranked, 1) if j in own), None)
        if hit and hit <= top_k:
            recall += 1
        if hit:
            rr += 1.0 / hit
    if nq:
        print(f"  自召回 Recall@{top_k}={recall/nq*100:.0f}%  MRR={rr/nq:.2f}（{nq} 次查询）")


def main():
    ap = argparse.ArgumentParser(description="记忆回测（只读源库）")
    ap.add_argument("--limit", type=int, default=40, help="离线上限；在线样本数")
    ap.add_argument("--source-db", default=None, help="源库路径（默认 stock_memory.db）")
    ap.add_argument("--with-embedding", action="store_true", help="启用在线（批量 embedding）")
    ap.add_argument("--max-chunks", type=int, default=120, help="在线最大 chunk 数（控额度）")
    ap.add_argument("--top-k", type=int, default=5, help="Recall@k 的 k")
    args = ap.parse_args()

    if args.source_db:
        src = args.source_db
    else:
        from utils.app_paths import get_stock_memory_db_path
        src = get_stock_memory_db_path()
    print(f"源库（只读）: {src}")
    rows = _load_reports(src)
    groups = _group_reports(rows)
    print(f"chunk 行={len(rows)} → 归组报告={len(groups)}（非 superseded）")
    groups = groups[:args.limit]

    phase_segment(groups)
    phase_chunk(groups)
    if args.with_embedding:
        phase_embedding(groups, args.limit, args.max_chunks, args.top_k)


if __name__ == "__main__":
    main()
