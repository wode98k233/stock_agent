"""
记忆管理路由：/api/memory/*
提供记忆系统的统计、搜索、清理功能，供管理后台和 CLI 使用。
"""
import logging
from typing import Optional
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from memory.episodic import EpisodicMemory

from server.deps import get_web_state

logger = logging.getLogger(__name__)

router = APIRouter()


class CleanRequest(BaseModel):
    before: Optional[str] = None      # ISO date string
    stock_code: Optional[str] = None
    dry_run: bool = True


def _get_sdk(request: Request):
    """从 WebAppState 获取 MemorySDK 实例"""
    web = get_web_state(request)
    sdk = getattr(web, "memory_sdk", None)
    if sdk is None:
        raise HTTPException(status_code=503, detail="记忆系统未初始化")
    return sdk


# ── 统计 ────────────────────────────────────────────────

def _run_in_thread(fn):
    """在线程池中执行同步阻塞操作，避免卡住 event loop。"""
    import asyncio
    return asyncio.get_running_loop().run_in_executor(None, fn)


@router.get("/api/memory/stats")
async def memory_stats(request: Request):
    """记忆系统统计概览（ChromaDB 统计可能慢，丢线程池）"""
    sdk = _get_sdk(request)
    return await _run_in_thread(sdk.get_stats)


# ── 搜索 ────────────────────────────────────────────────

@router.get("/api/memory/search")
async def memory_search(
    request: Request,
    q: str = "",
    type: str = "all",      # all | semantic | episodic
    top_k: int = 20,
):
    """搜索记忆条目"""
    if not q:
        return {"total": 0, "items": []}
    sdk = _get_sdk(request)
    items = await _run_in_thread(lambda: sdk.search(q, top_k=top_k, memory_type=type))
    return {"total": len(items), "items": items}


# ── 单条删除 ──────────────────────────────────────────

@router.delete("/api/memory/entries/{entry_id}")
async def memory_delete_entry(request: Request, entry_id: str):
    """删除单条记忆（语义+情景都尝试）"""
    sdk = _get_sdk(request)
    def _del():
        ok1 = sdk.backend.delete(entry_id)
        ok2 = EpisodicMemory(sdk._config.memory_db_path).delete(entry_id)
        return ok1 or ok2
    return {"success": await _run_in_thread(_del)}


# ── 按股票查询 ──────────────────────────────────────────

@router.get("/api/memory/stocks/{stock_code}")
async def memory_by_stock(request: Request, stock_code: str, limit: int = 50):
    """按股票代码查看历史记忆"""
    sdk = _get_sdk(request)
    items = await _run_in_thread(lambda: sdk.get_by_stock(stock_code, limit=limit))
    return {"stock_code": stock_code, "total": len(items), "items": items}


# ── 分页列表 ────────────────────────────────────────────

@router.get("/api/memory/entries")
async def memory_entries(request: Request, limit: int = 50, offset: int = 0):
    """分页列出所有记忆条目"""
    sdk = _get_sdk(request)
    items = await _run_in_thread(lambda: sdk.list_all(limit=limit, offset=offset))
    return {"total": len(items), "items": items}


# ── 手动验证延长 TTL（v2 M2） ──────────────────────────

@router.post("/api/memory/entries/{entry_id}/validate")
async def memory_validate(request: Request, entry_id: str):
    """手动验证某条记忆仍有效，更新 last_validated_at 并延长 TTL。"""
    sdk = _get_sdk(request)
    ok = await _run_in_thread(lambda: sdk.validate(entry_id))
    return {"entry_id": entry_id, "success": ok}


# ── 结论演进历史（v2 M3） ────────────────────────────────

@router.get("/api/memory/stocks/{stock_code}/history")
async def memory_conclusion_history(request: Request, stock_code: str):
    """查询某标的的完整结论演进链（当前 + 历史 + 演进路径）。"""
    sdk = _get_sdk(request)
    return await _run_in_thread(lambda: sdk.get_conclusion_history(stock_code))


# ── 来源血统溯源（v2） ──────────────────────────────────

@router.get("/api/memory/entries/{entry_id}/provenance")
async def memory_provenance(request: Request, entry_id: str):
    """查询某条记忆的来源血统：工具调用、推理路径、trace 反查结果。"""
    sdk = _get_sdk(request)
    return await _run_in_thread(lambda: sdk.get_provenance(entry_id))


# ── 清理 ────────────────────────────────────────────────

@router.delete("/api/memory/clean")
async def memory_clean(request: Request, payload: CleanRequest):
    """清理记忆（支持 dry_run 预览）"""
    sdk = _get_sdk(request)
    def _clean():
        return sdk.clean(before=payload.before, stock_code=payload.stock_code, dry_run=payload.dry_run)
    return await _run_in_thread(_clean)


# ── 全量清空 ────────────────────────────────────────────

@router.delete("/api/memory/reset")
async def memory_reset(request: Request):
    """清空全部记忆：语义记忆 + 情景记忆 + 向量数据 + 用户画像"""
    sdk = _get_sdk(request)
    return await _run_in_thread(sdk.reset_all)


# ── 后端信息 ────────────────────────────────────────────

class MemoryConfigUpdateRequest(BaseModel):
    min_similarity: Optional[float] = None  # 语义相似度门槛，[0, 1]，0 表示关闭


@router.get("/api/memory/backend")
async def memory_backend_info(request: Request):
    """当前检索后端信息"""
    sdk = _get_sdk(request)
    backend = sdk.backend
    return {
        "name": backend.name(),
        "available": backend.is_available(),
        "count": backend.count(),
    }


# ── 记忆系统配置（运行时可调，持久化到 memory_config.json） ──

@router.get("/api/memory/config")
async def memory_config_get(request: Request):
    """读取记忆系统运行时配置（min_similarity 等）。

    注意：与系统配置（/api/config）分离，记忆系统的语义相似度门槛
    不进入系统配置 schema，避免与 LLM/Agent 等配置混在一起。
    """
    sdk = _get_sdk(request)
    return {
        "min_similarity": sdk.get_min_similarity(),
        "retrieval_top_k": getattr(sdk._config, "retrieval_top_k", None),
        "backend": sdk.backend.name(),
    }


@router.post("/api/memory/config")
async def memory_config_update(request: Request, payload: MemoryConfigUpdateRequest):
    """更新记忆系统运行时配置（当前支持 min_similarity）。"""
    sdk = _get_sdk(request)
    updated = {}
    if payload.min_similarity is not None:
        try:
            value = sdk.set_min_similarity(payload.min_similarity)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        updated["min_similarity"] = value
    return {"success": True, **updated}


# ── 用户画像 ────────────────────────────────────────────

class ProfileUpdateRequest(BaseModel):
    risk_tolerance: str = ""        # conservative / moderate / aggressive
    investment_horizon: str = ""    # short / medium / long
    analysis_style: str = ""        # technical_heavy / fundamental_heavy / balanced


@router.get("/api/memory/profile")
async def memory_profile_get(request: Request):
    """获取用户画像"""
    from memory.user_profile import UserProfileManager
    pm = UserProfileManager(user_id="default")
    p = pm.profile
    return {
        "holdings": [{"code": h.stock_code, "name": h.stock_name, "added": h.added_at} for h in p.holdings],
        "risk_tolerance": p.risk_tolerance,
        "investment_horizon": p.investment_horizon,
        "analysis_style": p.analysis_style,
        "watched_sectors": dict(sorted(p.watched_sectors.items(), key=lambda x: x[1], reverse=True)[:10]),
        "watched_stocks": dict(sorted(p.watched_stocks.items(), key=lambda x: x[1], reverse=True)[:10]),
        "recent_queries": [{"query": q["query"], "date": q.get("date", "")} for q in p.recent_queries[-10:]],
        "preferred_indicators": p.preferred_indicators,
        "last_updated": p.last_updated,
    }


@router.post("/api/memory/profile")
async def memory_profile_update(request: Request, payload: ProfileUpdateRequest):
    """更新用户画像偏好"""
    from memory.user_profile import UserProfileManager
    pm = UserProfileManager(user_id="default")
    if payload.risk_tolerance:
        pm.set_risk_tolerance(payload.risk_tolerance)
    if payload.investment_horizon:
        pm.set_investment_horizon(payload.investment_horizon)
    if payload.analysis_style:
        pm.set_analysis_style(payload.analysis_style)
    return {"status": "ok", "updated": pm.profile.last_updated}


# ── 调试工具 ────────────────────────────────────────────

class EmbedRequest(BaseModel):
    text: str = ""


class TokenizeRequest(BaseModel):
    text: str = ""


class SimilarityRequest(BaseModel):
    text1: str = ""
    text2: str = ""


def _get_embedding_fn_direct():
    """直接从 Config 构建 embedding_fn，不依赖后端实例。
    即使启动时 embedding 服务未就绪导致后端降级为纯 FTS5，
    这里仍能独立测试 embedding 连通性。
    返回 (embedding_fn, config_info_dict) 或 (None, error_dict)。
    """
    try:
        from config import Config
        from memory.sdk import _build_embedding_fn
        fn = _build_embedding_fn()
        if fn is None:
            return None, {"error": "未配置 Embedding (STOCK_MEMORY_EMBEDDING 为空)"}
        return fn, {
            "mode": Config.STOCK_MEMORY_EMBEDDING,
            "model": Config.STOCK_MEMORY_EMBEDDING_MODEL,
            "api_base": Config.STOCK_MEMORY_API_BASE or Config.OPENAI_API_BASE,
        }
    except Exception as e:
        return None, {"error": f"构建 embedding_fn 失败: {e}"}




async def _call_embedding(fn, *texts):
    """在线程池中调用 embedding_fn，超时取自 STOCK_MEMORY_EMBEDDING_READY_TIMEOUT。"""
    from config import Config
    timeout = Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT
    loop = asyncio.get_running_loop()

    if len(texts) == 1:
        return await asyncio.wait_for(
            loop.run_in_executor(None, fn, texts[0]), timeout
        )
    else:
        async def _multi():
            results = await asyncio.gather(*(
                loop.run_in_executor(None, fn, t) for t in texts
            ))
            return results
        return await asyncio.wait_for(_multi(), timeout)


@router.post("/api/memory/debug/embed")
async def memory_debug_embed(payload: EmbedRequest):
    """测试 embedding：输出向量维度、前 10 个值（异步，不阻塞 event loop）"""
    from config import Config
    fn, info = _get_embedding_fn_direct()
    if fn is None:
        return info

    try:
        import time
        t0 = time.time()
        vec = await _call_embedding(fn, payload.text)
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            **info,
            "text": payload.text[:100],
            "dim": len(vec),
            "first_10": [round(float(v), 6) for v in vec[:10]],
            "last_10": [round(float(v), 6) for v in vec[-10:]],
            "norm": round(sum(v * v for v in vec) ** 0.5, 6),
            "elapsed_ms": elapsed_ms,
        }
    except asyncio.TimeoutError:
        return {**info, "error": f"Embedding 调用超时 ({Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT}s)"}
    except Exception as e:
        return {**info, "error": f"{type(e).__name__}: {e}"}


class RetrieveRequest(BaseModel):
    query: str = ""
    top_k: int = 10
    min_similarity: Optional[float] = None  # 可选：覆盖默认语义相似度门槛


@router.get("/api/memory/debug/embed-stats")
async def memory_debug_embed_stats(request: Request):
    """Embedding 统计仪表盘：向量库概览 + 条目明细"""
    sdk = _get_sdk(request)
    backend = sdk.backend
    emb = getattr(backend, "_embedding", backend)
    if not getattr(emb, "is_available", lambda: False)():
        return {"available": False}

    try:
        total = emb.count()
        data = emb._collection.get(
            include=["metadatas", "documents"],
            limit=min(total, 200),
        )
        metas = data.get("metadatas", []) or []
        ids_list = data.get("ids", []) or []
        docs_list = data.get("documents", []) or []

        # 股票/日期聚合
        stock_counts: dict = {}
        date_counts: dict = {}
        items = []
        for i in range(len(ids_list)):
            m = metas[i] if i < len(metas) else {}
            doc = docs_list[i] if i < len(docs_list) else ""
            code = m.get("stock_code", "") or ""
            name = m.get("stock_name", "") or ""
            d = m.get("date", "")[:10]
            tags_raw = m.get("tags", "")
            tags = []
            if tags_raw:
                try: tags = __import__('json').loads(tags_raw)
                except Exception: pass

            if code:
                key = f"{name}({code})" if name else code
                stock_counts[key] = stock_counts.get(key, 0) + 1
            if d:
                date_counts[d] = date_counts.get(d, 0) + 1

            items.append({
                "entry_id": ids_list[i],
                "stock_code": code,
                "stock_name": name,
                "date": d,
                "content": doc[:80],
                "tags": tags[:3],
                "source_query": m.get("source_query", "")[:60],
            })

        return {
            "available": True,
            "total_vectors": total,
            "top_stocks": dict(sorted(stock_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
            "date_distribution": dict(sorted(date_counts.items())),
            "items": items,
        }
    except Exception as e:
        return {"available": True, "error": str(e)}


@router.post("/api/memory/debug/retrieve")
async def memory_debug_retrieve(request: Request, payload: RetrieveRequest):
    """检索模拟：输入问题，返回 ChromaDB 中匹配的记忆及相似度排名"""
    sdk = _get_sdk(request)
    fn, info = _get_embedding_fn_direct()
    if fn is None:
        return info

    emb = getattr(sdk.backend, "_embedding", None)
    if emb is None or not emb.is_available():
        return {"error": "Embedding 后端不可用"}

    try:
        import time
        t0 = time.time()
        query_vec = fn(payload.query)
        # 检索时取比 top_k 更多的候选，便于在应用门槛后仍能返回足够条数
        query_top_k = max(payload.top_k, 30)
        results = emb._collection.query(
            query_embeddings=[query_vec],
            n_results=query_top_k,
            include=["metadatas", "documents", "distances"],
        )
        elapsed_ms = int((time.time() - t0) * 1000)

        # 语义相似度门槛：优先用请求参数，否则用 SDK 当前配置
        min_sim = payload.min_similarity if payload.min_similarity is not None \
            else (sdk.get_min_similarity() if hasattr(sdk, "get_min_similarity") else 0.0)

        items = []
        ids_list = results.get("ids", [[]])[0]
        metas_list = results.get("metadatas", [[]])[0] or []
        docs_list = results.get("documents", [[]])[0] or []
        dists_list = results.get("distances", [[]])[0] or []

        for i, eid in enumerate(ids_list):
            meta = metas_list[i] if i < len(metas_list) else {}
            doc = docs_list[i] if i < len(docs_list) else ""
            dist = dists_list[i] if i < len(dists_list) else 1.0
            sim = max(0.0, 1.0 - dist / 2.0)
            items.append({
                "entry_id": eid,
                "stock": f"{meta.get('stock_name','')}({meta.get('stock_code','')})",
                "date": meta.get("date", "")[:16],
                "content": doc[:150],
                "similarity": round(sim, 4),
                "passes_threshold": (sim >= min_sim) if min_sim and min_sim > 0 else True,
            })

        # 应用语义相似度门槛（与真实检索链路一致）
        raw_found = len(items)
        if min_sim and min_sim > 0:
            items = [it for it in items if it["similarity"] >= min_sim]
        items = items[:payload.top_k]

        return {
            **info,
            "query": payload.query,
            "total_in_db": emb.count(),
            "found": len(items),
            "raw_found": raw_found,
            "min_similarity": min_sim,
            "elapsed_ms": elapsed_ms,
            "simulate_top_k": sdk._config.retrieval_top_k if hasattr(sdk, "_config") else 5,
            "items": items,
        }
    except Exception as e:
        return {**info, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/memory/debug/tokenize")
async def memory_debug_tokenize(payload: TokenizeRequest):
    """展示 jieba 分词结果"""
    try:
        import jieba
        tokens = list(jieba.cut(payload.text))
        return {
            "text": payload.text,
            "tokens": tokens,
            "count": len(tokens),
        }
    except ImportError:
        return {"error": "jieba 未安装"}
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/memory/debug/similarity")
async def memory_debug_similarity(payload: SimilarityRequest):
    """计算两段文本的 embedding 余弦相似度（异步，不阻塞 event loop）"""
    from config import Config
    fn, info = _get_embedding_fn_direct()
    if fn is None:
        return info

    try:
        v1, v2 = await _call_embedding(fn, payload.text1, payload.text2)
        dot = sum(a * b for a, b in zip(v1, v2))
        norm1 = sum(a * a for a in v1) ** 0.5
        norm2 = sum(b * b for b in v2) ** 0.5
        sim = dot / (norm1 * norm2) if norm1 > 0 and norm2 > 0 else 0.0
        return {
            **info,
            "similarity": round(sim, 6),
            "dim": len(v1),
        }
    except asyncio.TimeoutError:
        return {**info, "error": f"Embedding 调用超时 ({Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT}s)"}
    except Exception as e:
        return {**info, "error": f"{type(e).__name__}: {e}"}
