"""
记忆系统 — Memory System
============================================================
跨会话的语义记忆、情景记忆、用户画像管理。

快速入门
--------
- 唯一对外入口: from memory.sdk import MemorySDK, get_sdk
- 数据模型:     from memory.backend import MemoryEntry, MemoryBackend
- 请求上下文:   from memory.context import current_session_id

架构总览
--------
写入链路 (archive):
  agent 执行完 → SDK.archive()
    → metadata.collect_metadata()         提取 PE/行业/tags 等硬数据
    → chunking.chunk_report()             报告文本切分
    → provenance.ProvenanceBuilder        构建来源血统
    → confidence.ConfidenceCalculator     计算置信度 (base×freshness×feedback×corroboration)
    → freshness.FreshnessManager          分类 + 设 TTL 过期时间
    → merge.Merger                        检测冲突 + 标记旧记忆 superseded
    → backend.add()                       写入 FTS5 + ChromaDB 向量库
    → episodic.EpisodicMemory.log()       记录情景事件

检索链路 (retrieve):
  用户发问题 → SDK.retrieve()
    → backend.search()                    FTS5 BM25 + Embedding RRF 融合 + Reranker 精排
    → confidence 加权                     final = relevance × confidence
    → freshness 过滤                      剔除过期记忆
    → merge 过滤                          排除已被取代的旧结论
    → prompt 格式化注入                    🟢高信 🟡中信 🔴低信 标签

模块清单
--------
核心入口:
  sdk.py          唯一对外门面 — MemorySDK / get_sdk()

数据层:
  backend/        检索后端策略体系
    base.py         MemoryEntry 数据模型 + MemoryBackend 抽象基类
    fts5.py         SQLite FTS5 + jieba 分词全文检索
    embedding.py    ChromaDB 向量语义检索
    hybrid.py       RRF 融合 + Cross-encoder Reranker + 工厂函数
  context.py      请求上下文 (session_id / dialog_uuid / trace_run_id)
  episodic.py     情景记忆事件日志 (episode_log 表)

v2 分析能力:
  metadata.py     元数据提取 (工具JSON → PE/PB/sector; 仪表盘 → sentiment/tags)
  chunking.py     报告文本切分 (markdown 标题 > 空行 > 单空行, 80-1000字窗口)
  confidence.py   置信度评分 (v2 M1) — 4 因子合成: base × freshness × feedback × corroboration
  provenance.py   来源血统 (v2 M1) — 工具调用链 + trace 反查
  freshness.py    时效性管理 (v2 M2) — 9 种记忆分类 + TTL + 自动过期清理
  merge.py        合并/冲突消解 (v2 M3) — 同标的 supersede + conflict_log + 演进链

辅助:
  semantic.py     语义记忆封装 (SemanticMemory)
  user_profile.py 用户画像管理 (UserProfileManager)
"""

# ── 公共 API 懒加载 re-export ────────────────────────────
# 避免启动期导入所有重型依赖（chromadb, sentence_transformers 等），
# 只在首次访问时才加载对应模块。


def __getattr__(name: str):
    """延迟导入，在 from memory import X 时触发。"""
    _EXPORTS = {
        # 核心门面
        "MemorySDK": "memory.sdk",
        "MemoryConfig": "memory.sdk",
        "get_sdk": "memory.sdk",
        # 数据模型
        "MemoryEntry": "memory.backend.base",
        "MemoryBackend": "memory.backend.base",
        # v2 能力
        "ConfidenceCalculator": "memory.confidence",
        "ProvenanceBuilder": "memory.provenance",
        "ProvenanceQuerier": "memory.provenance",
        "FreshnessManager": "memory.freshness",
        "Merger": "memory.merge",
        # 元数据
        "collect_metadata": "memory.metadata",
        "classify_query_subject": "memory.metadata",
        # 情景记忆
        "EpisodicMemory": "memory.episodic",
        # 请求上下文
        "current_session_id": "memory.context",
        "current_dialog_uuid": "memory.context",
        "current_trace_run_id": "memory.context",
    }
    if name in _EXPORTS:
        import importlib
        mod = importlib.import_module(_EXPORTS[name])
        attr = getattr(mod, name)
        # 缓存到模块全局，后续访问不再走 __getattr__
        globals()[name] = attr
        return attr
    raise AttributeError(f"module 'memory' has no attribute '{name}'")


def __dir__():
    """补全 IDE 自动补全。"""
    return list(__getattr__._EXPORTS.keys())  # type: ignore[attr-defined]


# 缓存 _EXPORTS 供 __dir__ 引用
__getattr__._EXPORTS = {  # type: ignore[attr-defined]
    "MemorySDK": "memory.sdk",
    "MemoryConfig": "memory.sdk",
    "get_sdk": "memory.sdk",
    "MemoryEntry": "memory.backend.base",
    "MemoryBackend": "memory.backend.base",
    "ConfidenceCalculator": "memory.confidence",
    "ProvenanceBuilder": "memory.provenance",
    "ProvenanceQuerier": "memory.provenance",
    "FreshnessManager": "memory.freshness",
    "Merger": "memory.merge",
    "collect_metadata": "memory.metadata",
    "classify_query_subject": "memory.metadata",
    "EpisodicMemory": "memory.episodic",
    "current_session_id": "memory.context",
    "current_dialog_uuid": "memory.context",
    "current_trace_run_id": "memory.context",
}

