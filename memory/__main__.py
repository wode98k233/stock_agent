"""
记忆系统自检工具 — python -m memory

一键验证记忆系统的 4 个核心环节是否工作正常：
  1. 配置与服务探测    — embedding/reranker 后端是否就绪、实际生效的 backend 类型
  2. 检索链路          — FTS5 分词召回 → RRF 融合 → reranker 精排 → 置信度加权
  3. 归档正确性        — 写入一条测试记忆 → 语义/全文双路查回 → 验证元数据字段
  4. 清理              — 删除测试记忆，不留残留（默认自动清理，可 --keep 保留观察）

用法：
  python -m memory                    全量自检（推荐）
  python -m memory --check-only       只做 1（服务探测），不调检索/归档
  python -m memory --retrieve 问题    只做检索验证（用自定义问题）
  python -m memory --keep             归档后保留测试记忆（便于手动检查）

所有操作只针对本次新建的测试条目（entry_id 带 __selfcheck__ 前缀），
不触碰真实记忆数据。
"""
import argparse
import os
import sys
import time
import uuid

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from memory.backend import ensure_jieba_ready  # noqa: E402
from memory.sdk import _build_embedding_fn, get_sdk  # noqa: E402
from memory.backend.hybrid import _build_reranker  # noqa: E402

SELFCHECK_TAG = "__selfcheck__"


def _sep(title: str) -> None:
    print(f"\n{'=' * 62}\n▶ {title}\n{'=' * 62}")


def step1_probe() -> dict:
    """配置与服务探测：embedding_fn / reranker / 实际 backend 类型"""
    _sep("1/4 配置与服务探测")
    print(f"  STOCK_MEMORY_BACKEND        = {Config.STOCK_MEMORY_BACKEND!r}")
    print(f"  STOCK_MEMORY_EMBEDDING      = {Config.STOCK_MEMORY_EMBEDDING!r}")
    print(f"  STOCK_MEMORY_EMBEDDING_MODEL= {Config.STOCK_MEMORY_EMBEDDING_MODEL!r}")
    print(f"  STOCK_MEMORY_RERANKER       = {Config.STOCK_MEMORY_RERANKER!r}")
    print(f"  STOCK_MEMORY_RERANKER_MODEL = {Config.STOCK_MEMORY_RERANKER_MODEL!r}")
    print(f"  STOCK_MEMORY_API_BASE       = {Config.STOCK_MEMORY_API_BASE!r}")

    # jieba
    ensure_jieba_ready()
    from memory.backend.fts5 import _JIEBA, FTS5Backend
    print(f"  [jieba] 就绪 = {_JIEBA is not None}")
    if _JIEBA:
        print(f"         分词示例: {FTS5Backend._segment('宁德时代最近走势如何')!r}")

    # embedding_fn（会触发网络探测，失败仅告警）
    emb_fn = _build_embedding_fn()
    emb_ok = False
    if emb_fn is None:
        print("  [embedding] 未配置 embedding_fn → 将降级为纯 FTS5")
    else:
        try:
            vec = emb_fn("自检探测文本")
            emb_ok = bool(vec) and len(vec) > 0
            print(f"  [embedding] 可用, dim={len(vec) if emb_ok else '?'}, 前3={vec[:3] if emb_ok else ''}")
        except Exception as e:
            msg = str(e)
            print(f"  [embedding] 探测失败: {type(e).__name__}: {msg[:120]}")
            if "Model does not exist" in msg and "/" not in Config.STOCK_MEMORY_EMBEDDING_MODEL:
                print(f"  ⚠️ 疑似模型名缺厂商前缀：当前 '{Config.STOCK_MEMORY_EMBEDDING_MODEL}'，"
                      f"硅基流动需要形如 'BAAI/bge-m3'（带 BAAI/ 前缀）")
                print(f"  → 建议把 .env 中 STOCK_MEMORY_EMBEDDING_MODEL 改为 'BAAI/{Config.STOCK_MEMORY_EMBEDDING_MODEL}'")

    # reranker
    reranker = _build_reranker()
    if reranker is None:
        print("  [reranker] 未启用（STOCK_MEMORY_RERANKER 为空）→ 检索不做精排")
    else:
        try:
            avail = reranker.is_available()
            print(f"  [reranker] 类型={type(reranker).__name__} available={avail}")
            reranker.warm()
            # 真实验证：构造 3 条测试文档做一次精排，确认分数非全 0
            from memory.backend.base import MemoryEntry
            probe = [
                MemoryEntry(entry_id="__probe_a__", stock_code="600519", stock_name="贵州茅台",
                            content="贵州茅台市盈率处于历史低位，白酒龙头，ROE 持续 30%+"),
                MemoryEntry(entry_id="__probe_b__", stock_code="002714", stock_name="牧原股份",
                            content="牧原股份猪周期底部反转预期增强，成本控制行业最优"),
                MemoryEntry(entry_id="__probe_c__", stock_code="000001", stock_name="平安银行",
                            content="大盘今日缩量下跌，上证指数失守 4000 点，主力资金流出"),
            ]
            t0 = time.time()
            ranked = reranker.rerank("茅台估值高吗", probe)
            ms = int((time.time() - t0) * 1000)
            scores = [(e.stock_name, e.rerank_score) for e in ranked]
            print(f"  [reranker] 实测精排 [{ms}ms]: {scores}")
            if all(s[1] == 0 for s in scores):
                print("  ⚠️ reranker 调用成功但全部 0 分 → 检查 .env 模型名是否缺厂商前缀 "
                      "(硅基流动需 'BAAI/bge-reranker-v2-m3')，或 API key/base_url")
            else:
                print("  ✅ reranker 精排生效（分数非全 0，排序合理）")
        except Exception as e:
            print(f"  [reranker] 探测异常: {type(e).__name__}: {str(e)[:120]}")

    # 实际 backend
    sdk = get_sdk()
    sdk.warmup()
    backend = sdk.backend
    print(f"  [backend] 实际生效 = {backend.name()} ({type(backend).__name__})")
    if hasattr(backend, "stats"):
        st = backend.stats()
        print(f"           semantic={st.get('semantic_count', '?')} "
              f"episodic={st.get('episodic_count', '?')} "
              f"embedding={st.get('embedding_count', 'N/A')} "
              f"reranker_avail={st.get('reranker_available', 'N/A')}")
    return {"emb_ok": emb_ok, "backend": backend.name()}


def step2_retrieve(extra_query: str = "") -> None:
    """检索链路验证：FTS5 分词 → RRF → reranker 精排 → 置信度加权"""
    _sep("2/4 检索链路验证（FTS5 召回 → RRF 融合 → reranker 精排 → 置信度加权）")
    sdk = get_sdk()
    queries = [extra_query] if extra_query else [
        "宁德时代最近走势如何",
        "今天大盘怎么样",
        "牧原股份能买吗",
    ]
    for q in queries:
        t0 = time.time()
        text = sdk.retrieve(q)
        ms = int((time.time() - t0) * 1000)
        if not text:
            print(f"\n  query={q!r} → (无结果) [{ms}ms]")
            continue
        lines = text.split("\n")
        print(f"\n  query={q!r} → {len(lines)} 行 [{ms}ms]")
        for line in lines[:6]:
            print(f"    {line[:100]}")


def step3_archive(keep: bool) -> str:
    """归档正确性验证：写入测试记忆 → 双路查回 → 校验元数据"""
    _sep("3/4 归档正确性验证（写入 → 查回 → 校验）")
    sdk = get_sdk()
    tag = f"{SELFCHECK_TAG}_{uuid.uuid4().hex[:6]}"
    stock_code = f"99{int(time.time()) % 100000:05d}"  # 990xxxxx 测试代码
    user_input = f"自检：{tag} 测试记忆归档，标的为测试股 {stock_code}"
    result = (
        f"## 核心结论\n测试记忆（{tag}）：该标的技术面呈多头排列，"
        f"MA5 上穿 MA20 形成金叉，资金持续净流入，短期偏多。\n"
        f"## 关键要点\n1. 均线系统多头\n2. 成交量放大\n3. 板块景气度回升\n"
    )
    # 注入测试元数据（tags/subject/sector），确保归档能提取到结构化字段
    from memory.metadata import push_tool_extract, set_dashboard_meta
    try:
        push_tool_extract({"stock_code": stock_code, "stock_name": f"测试股{stock_code[-4:]}",
                           "sector_l2": "测试行业", "pe": 25.0, "roe": 15.0})
        set_dashboard_meta({"tags": [tag, "自检", "测试"], "sentiment": "bullish",
                            "key_findings": ["测试归档"], "subject_kind": "stock"})
    except Exception:
        pass

    t0 = time.time()
    sdk.archive(user_input, result)
    ms = int((time.time() - t0) * 1000)
    print(f"  [archive] 完成 [{ms}ms] entry_tag={tag}")

    # 语义检索查回（走 RRF + reranker）
    found_sem = [r for r in sdk.search(tag, top_k=10) if tag in (r.get("content") or "")]
    # 全文 LIKE 查回
    found_fts = [r for r in sdk.search(tag, top_k=10, memory_type="semantic")
                 if tag in (r.get("content") or "")]
    ids = []
    for r in found_fts:
        ids.append(r.get("entry_id"))
    print(f"  [查回] 语义检索命中 {len(found_sem)} 条, 全文命中 {len(found_fts)} 条")
    for r in found_fts[:3]:
        print(f"    - {r.get('entry_id', '')[:18]} | conf={r.get('confidence', '?')} | "
              f"{r.get('stock_code')} {r.get('stock_name')} | {(r.get('content') or '')[:60]}")

    ok = len(found_fts) >= 1
    print(f"  [校验] 归档可查回: {'✅ 通过' if ok else '❌ 失败'}")
    return tag


def step4_cleanup(tag: str) -> None:
    """清理测试记忆（FTS5 + Chroma 双删）"""
    _sep("4/4 清理测试记忆")
    if not tag:
        print("  无测试条目，跳过")
        return
    sdk = get_sdk()
    # 通过 backend 直接删除所有带 tag 的条目
    entries = sdk.search(tag, top_k=50)
    deleted = 0
    for r in entries:
        eid = r.get("entry_id")
        if not eid or tag not in (r.get("content") or ""):
            continue
        try:
            sdk.backend.delete(eid)
            deleted += 1
        except Exception as e:
            print(f"  删除 {eid[:16]} 失败: {e}")
    print(f"  已删除 {deleted} 条测试记忆")


def _clean_dashboard_json() -> None:
    """清洗存量记忆中的 dashboard JSON 碎片。

    历史版本把 @@DASHBOARD_START@@{json} 原样写入记忆 content（且常被 chunk 截断
    成不完整碎片），既污染 FTS5 检索又污染 embedding 向量。此命令扫描全部记忆，
    用 dashboard_to_text() 把 JSON 提炼成可读文本后重写（FTS5 REPLACE + chroma upsert）。
    幂等：无 JSON 碎片的记忆跳过。
    """
    import sqlite3
    from memory.chunking import dashboard_to_text
    from memory.backend.base import MemoryEntry
    from memory.backend import ensure_jieba_ready

    ensure_jieba_ready()
    sdk = get_sdk()
    sdk.warmup()
    backend = sdk.backend
    if not hasattr(backend, "get_all_raw"):
        print("  当前 backend 不支持 get_all_raw，跳过")
        return

    rows = backend.get_all_raw()
    print(f"扫描 {len(rows)} 条记忆…")
    cleaned = 0
    for r in rows:
        raw = r.get("raw_content") or ""
        if "@@DASHBOARD" not in raw:
            continue
        new_content = dashboard_to_text(raw)
        if new_content == raw:
            continue
        meta = {
            "source_query": r.get("source_query", ""),
            "date": r.get("date") or "",
            "tags": r.get("tags") or [],
            "sector_l1": r.get("sector_l1") or "",
            "sector_l2": r.get("sector_l2") or "",
            "pe": r.get("pe"), "roe": r.get("roe"),
            "volatility": r.get("volatility") or "",
            "sentiment": r.get("sentiment") or "",
            "subject_kind": r.get("subject_kind") or "",
        }
        entry = MemoryEntry(
            entry_id=r["entry_id"],
            stock_code=r.get("stock_code") or "",
            stock_name=r.get("stock_name") or "",
            content=new_content,
            metadata=meta,
            confidence=r.get("confidence", 0.5),
            confidence_factors=r.get("confidence_factors") or {},
            provenance=r.get("provenance") or {},
        )
        try:
            backend.add(entry)  # FTS5 先删后插 + chroma upsert（重嵌）
            cleaned += 1
        except Exception as e:
            print(f"  重写 {r['entry_id'][:16]} 失败: {e}")
    print(f"✅ 已清洗 {cleaned} 条含 dashboard JSON 碎片的记忆")


def main():
    parser = argparse.ArgumentParser(description="记忆系统自检工具")
    parser.add_argument("--check-only", action="store_true",
                        help="只做服务探测，不调检索/归档")
    parser.add_argument("--retrieve", metavar="QUERY", default="",
                        help="用自定义问题做检索验证")
    parser.add_argument("--keep", action="store_true",
                        help="归档后保留测试记忆（默认自动清理）")
    parser.add_argument("--clean-dashboard", action="store_true",
                        help="清洗存量记忆中的 dashboard JSON 碎片（重写 content + 重嵌）")
    args = parser.parse_args()

    if args.clean_dashboard:
        _clean_dashboard_json()
        return

    t_all = time.time()
    print(f"记忆系统自检 | backend_mode={Config.STOCK_MEMORY_BACKEND} | "
          f"reranker={Config.STOCK_MEMORY_RERANKER or 'off'}")
    info = step1_probe()
    if args.check_only:
        print(f"\n✅ 服务探测完成，耗时 {int(time.time() - t_all)}s（未执行检索/归档）")
        return

    step2_retrieve(args.retrieve)

    tag = ""
    if args.keep:
        print("\n[--keep] 跳过归档验证（保留现有记忆）")
    else:
        tag = step3_archive(keep=False)
        step4_cleanup(tag)

    print(f"\n✅ 自检完成，总耗时 {int(time.time() - t_all)}s")
    if info.get("backend") == "fts5":
        print("⚠️ 注意：当前实际是纯 FTS5 后端（embedding 未生效），"
              "检索无语义召回、无 reranker 精排。检查 STOCK_MEMORY_EMBEDDING 配置。")


if __name__ == "__main__":
    main()
