"""
记忆系统 — 唯一对外 SDK 门面
CLI bootstrap 和 Web bootstrap 各自初始化一次，其他模块不直接引用内部实现。

用法:
    from memory.sdk import MemorySDK, MemoryConfig

    sdk = MemorySDK(MemoryConfig(
        backend_mode=Config.STOCK_MEMORY_BACKEND,
        embedding_fn=_build_embedding_fn(Config),
        user_id="default",
    ))

    # 检索（Agent 请求前）
    ctx = sdk.retrieve(user_input)

    # 归档（Agent 请求后）
    sdk.archive(user_input, result, exec_state)

    # 管理
    stats = sdk.get_stats()
    results = sdk.search("茅台估值")
    sdk.clean(before="2026-06-01", dry_run=False)
"""
import logging
import os
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict
from datetime import datetime

from config import Config
from utils.app_paths import (
    get_stock_memory_db_path,
    get_user_profile_dir,
    get_memory_config_path,
)
from memory.backend import MemoryBackend, MemoryEntry, create_memory_backend
from memory.chunking import (
    chunk_report, strip_report_decor, dashboard_to_text, strip_chart_markers,
)
from memory.confidence import ConfidenceCalculator
from memory.freshness import FreshnessManager
from memory.merge import Merger
from memory.provenance import ProvenanceBuilder
from memory.episodic import EpisodicMemory
from memory.context import (
    current_session_id, current_dialog_uuid, current_trace_run_id,
)
from memory.metadata import collect_metadata, classify_query_subject, reset as _reset_metadata

_log = logging.getLogger(__name__)

# 中文文本 1 token ≈ 1.5~2 字符（保守取 2）。`MEMORY_MAX_PROMPT_TOKENS` 是 token 预算，
# 而 `retrieve` 拼装的是**字符**，需要换算 —— 原实现直接拿 token 数当字符数截断。
_PROMPT_CHARS_PER_TOKEN = 2


def _truncate_at_boundary(text: str, limit: int) -> str:
    """按行/句边界截断文本，避免展示片段停在半句话/半行表格上。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    pos = max(cut.rfind("\n"), cut.rfind("。"), cut.rfind("；"),
              cut.rfind("！"), cut.rfind("？"))
    if pos > limit * 0.5:
        return cut[:pos + 1].strip()
    return cut.strip()


class _RadarLogAdapter:
    """把 RadarLogger（结构化 tag 风格）适配成 stdlib logging 风格（msg + % args）。

    ``MemorySDK.retrieve`` 内部按 stdlib 风格调用 ``logger.info(fmt, *args)``；
    当传入的是请求级 ``RadarLogger`` 时，本适配器转发为 ``radar.info("M", text)``，
    使 ``[retrieve]`` 统计落进**该请求**的日志文件（此前用模块 logger，看不到）。
    """
    def __init__(self, radar):
        self._r = radar

    @staticmethod
    def _fmt(msg, args):
        try:
            return msg % args if args else msg
        except Exception:
            return f"{msg} {args}"

    def info(self, msg, *args, **kv):
        self._r.info("M", self._fmt(msg, args))

    def debug(self, msg, *args, **kv):
        self._r.debug("M", self._fmt(msg, args))

    def warning(self, msg, *args, **kv):
        self._r.warning("M", self._fmt(msg, args))

    warn = warning

    def error(self, msg, *args, **kv):
        self._r.error("M", self._fmt(msg, args))

    def exception(self, msg, *args, **kv):
        self._r.error("M", self._fmt(msg, args), exc_info=True)


def _as_std_logger(logger):
    """把可选的 logger 归一为 stdlib 风格：RadarLogger → 适配器；None → 模块 logger。"""
    if logger is None:
        return _log
    try:
        from utils.logger import RadarLogger
        if isinstance(logger, RadarLogger):
            return _RadarLogAdapter(logger)
    except Exception:
        pass
    return logger


def _augment_query_with_subject(user_input: str, context: str = None) -> str:
    """检索用 query 的实体归一（R2，2026-09-23）。

    - 提问本身已含股票实体 → 原样返回；
    - 提问为代词/追问（如「这个股…」）且**会话上下文**含股票 → 追加「股票名 代码」帮助召回。
    """
    ui = (user_input or "").strip()
    if not ui:
        return user_input
    try:
        from memory.metadata import classify_query_subject
        subj = classify_query_subject(ui)
        if subj.get("subject_kind") == "stock":
            return ui
        if context:
            s2 = classify_query_subject(context)
            if s2.get("subject_kind") == "stock":
                extra = " ".join(x for x in (s2.get("stock_name"), s2.get("stock_code")) if x)
                if extra:
                    return f"{ui} {extra}"
    except Exception:
        pass
    return ui


def _is_incomplete_result(text: str) -> bool:
    """判断 agent 结果是否为「数据不足」失败残片。

    仅当文本含模板失败标记（如「⚠️(数据不足)」「数据不足，无法分析」）才判定，
    避免误伤正常报告里偶提「数据不足」三字的场景。
    """
    if not text:
        return False
    if "数据不足" not in text:
        return False
    markers = (
        "无法分析", "⚠️(数据不足)", "数据不足，无法", "数据不足,无法",
        "数据不足，暂", "数据不足，当前", "数据不足）",
    )
    return any(m in text for m in markers)


# ------------------------------------------------------------
# embedding_fn 工厂
# ------------------------------------------------------------

def _build_embedding_fn():
    """
    根据 Config 构建 embedding 函数。
    返回 None 表示未配置 embedding, 后端自动降级为 FTS5。
    """
    emb_type = Config.STOCK_MEMORY_EMBEDDING.strip()
    if not emb_type:
        return None

    # ── 统一网关路径（推荐）──
    # 通过 utils.llm 网关构造 embedding，享受「限流 / 重试 / 熔断 / 追踪」一体化，
    # 且 embed 不再是绕过 tracked_invoke 的旁路（见 LLM 网关设计规格）。
    # 网关不可用时（导入失败 / provider 构造失败）回退到下面的本地构造逻辑，保持兼容。
    try:
        from utils.llm import gateway, Purpose
        gateway._get_embed_provider()  # 构造期探测，失败则回退旧逻辑

        def _embed(text: str) -> list:
            return gateway.embed([text], purpose=Purpose.MEMORY)[0]

        return _embed
    except Exception:
        pass

    if emb_type == "openai" or emb_type == "remote":
        try:
            from openai import OpenAI
            import httpx

            api_key = Config.STOCK_MEMORY_API_KEY or Config.OPENAI_API_KEY
            base_url = Config.STOCK_MEMORY_API_BASE or Config.OPENAI_API_BASE
            if not api_key:
                return None

            # 内网地址不走系统代理
            client = OpenAI(
                api_key=api_key, base_url=base_url,
                timeout=Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT,
                http_client=httpx.Client(proxy=None),
            )
            model = Config.STOCK_MEMORY_EMBEDDING_MODEL

            def _embed(text: str) -> list:
                resp = client.embeddings.create(input=text, model=model)
                return resp.data[0].embedding

            return _embed
        except ImportError:
            return None
        except Exception:
            return None

    if emb_type == "ollama":
        # Ollama 原生 API: POST /api/embeddings, body 用 "prompt" 而非 "input"
        # 用 requests 直接调，不走系统代理（Ollama 是内网地址）
        try:
            import requests
            base_url = Config.STOCK_MEMORY_API_BASE or "http://localhost:11434"
            api_url = base_url.rstrip("/").removesuffix("/v1") + "/api/embeddings"
            model = Config.STOCK_MEMORY_EMBEDDING_MODEL
            timeout = Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT

            def _embed(text: str) -> list:
                resp = requests.post(
                    api_url,
                    json={"model": model, "prompt": text},
                    timeout=timeout,
                    proxies={"http": None, "https": None},  # 不走系统代理
                )
                resp.raise_for_status()
                return resp.json()["embedding"]

            return _embed
        except ImportError:
            return None
        except Exception:
            return None

    if emb_type == "local":
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(Config.STOCK_MEMORY_LOCAL_MODEL)

            def _embed(text: str) -> list:
                return model.encode(text).tolist()

            return _embed
        except ImportError:
            return None
        except Exception:
            return None

    return None



# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
# 语义相似度门槛默认值（自适应）：保留 score >= max(floor, top_score - margin)。
# 实测（2026-09-23）：固定 0.72 会把 bge-m3 上口语 query 的全部向量命中滤空
# （最高余弦仅 ~0.60），故改为「低绝对下限 + 相对带宽」。
MIN_SIMILARITY_DEFAULT = 0.30        # 绝对下限（floor）
SIMILARITY_MARGIN_DEFAULT = 0.12     # 相对带宽（margin）


def load_memory_config() -> dict:
    """读取记忆系统用户配置（min_similarity / similarity_margin 等运行时可调项）。

    文件不存在或格式错误时返回默认值，不抛异常。
    """
    defaults = {
        "min_similarity": MIN_SIMILARITY_DEFAULT,
        "similarity_margin": SIMILARITY_MARGIN_DEFAULT,
    }
    path = get_memory_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            defaults.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_memory_config(cfg: dict) -> None:
    """持久化记忆系统用户配置到 memory_config.json。"""
    path = get_memory_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


@dataclass
class MemoryConfig:
    """记忆系统配置，由 bootstrap 层一次性构造"""
    backend_mode: str = "auto"                   # auto / fts5 / embedding / hybrid
    embedding_fn: Optional[Callable] = None       # 文本→向量的可调用对象
    memory_db_path: Optional[str] = None          # stock_memory.db 路径
    user_id: str = "default"                      # 用户标识
    retrieval_top_k: int = 5                      # 检索最大条数
    max_prompt_tokens: int = 1500                 # 注入 prompt 的最大字符数
    max_prompt_chars: int = 6000                  # 注入字符预算（优先；0=回退 max_prompt_tokens×2）
    max_chunks_per_group: int = 6                 # 同一报告最多注入的 chunk 数
    chunk_max_chars: int = 1000                   # 单块展示字符上限
    episodic_retention_days: int = 90             # 情景记忆保留天数
    embedding_ready_timeout: float = 30.0         # 初始化时等待 embedding 服务就绪的最长秒数
    embedding_ready_interval: float = 1.5         # 初始化时探针轮询间隔秒数
    min_similarity: float = MIN_SIMILARITY_DEFAULT  # 语义相似度绝对下限（0 关闭）
    similarity_margin: float = SIMILARITY_MARGIN_DEFAULT  # 相对带宽：保留 top-margin 以内
    confidence_threshold: float = 0.4             # v2: 置信度门槛，低于此值的记忆不注入 prompt


# ------------------------------------------------------------
# SDK 门面
# ------------------------------------------------------------
class MemorySDK:
    """记忆系统唯一对外入口"""

    def __init__(self, config: MemoryConfig):
        self._config = config
        self._backend: Optional[MemoryBackend] = None
        self._initialized = False

    def _ensure_init(self):
        if self._initialized:
            return
        if self._config.memory_db_path is None:
            self._config.memory_db_path = get_stock_memory_db_path()
        self._backend = create_memory_backend(
            mode=self._config.backend_mode,
            embedding_fn=self._config.embedding_fn,
            db_path=self._config.memory_db_path,
            embedding_ready_timeout=self._config.embedding_ready_timeout,
            embedding_ready_interval=self._config.embedding_ready_interval,
            min_similarity=self._config.min_similarity,
            similarity_margin=self._config.similarity_margin,
        )
        self._initialized = True

    def warmup(self):
        """主动触发后端构建 + embedding 就绪探测（含最长等待）。

        bootstrap 层在启动时调用，把「等待 embedding 服务就绪」放到启动阶段，
        而不是塞进首个分析请求的链路里。重复调用安全（_ensure_init 有幂等保护）。
        """
        self._ensure_init()

    def set_min_similarity(self, value: float) -> float:
        """运行时调整语义相似度门槛并持久化（0 表示关闭）。

        Args:
            value: 余弦相似度门槛，必须落在 [0, 1] 区间。
        Returns:
            实际生效的门槛值。
        """
        value = float(value)
        if not (0.0 <= value <= 1.0):
            raise ValueError("min_similarity 必须落在 [0, 1] 区间")
        self._ensure_init()
        self._config.min_similarity = value
        # 同步到后端 embedding 实例（hybrid 模式下是 _embedding，纯 embedding 模式是 backend 自身）
        backend = self._backend
        if backend is not None:
            emb = getattr(backend, "_embedding", None)
            if emb is not None and hasattr(emb, "set_min_similarity"):
                emb.set_min_similarity(value)
            elif hasattr(backend, "set_min_similarity"):
                backend.set_min_similarity(value)
        # 持久化，重启后仍生效
        cfg = load_memory_config()
        cfg["min_similarity"] = value
        save_memory_config(cfg)
        return value

    def get_min_similarity(self) -> float:
        """读取当前语义相似度下限（floor）。"""
        return self._config.min_similarity

    def set_similarity_margin(self, value: float) -> float:
        """运行时调整语义相似度相对带宽（margin）并持久化（0 表示纯绝对门槛）。"""
        value = float(value)
        if value < 0:
            raise ValueError("similarity_margin 必须 >= 0")
        self._ensure_init()
        self._config.similarity_margin = value
        backend = self._backend
        if backend is not None:
            emb = getattr(backend, "_embedding", None)
            if emb is not None and hasattr(emb, "set_similarity_margin"):
                emb.set_similarity_margin(value)
            elif hasattr(backend, "set_similarity_margin"):
                backend.set_similarity_margin(value)
        cfg = load_memory_config()
        cfg["similarity_margin"] = value
        save_memory_config(cfg)
        return value

    def get_similarity_margin(self) -> float:
        """读取当前语义相似度相对带宽（margin）。"""
        return self._config.similarity_margin

    def _resolve_max_prompt_chars(self) -> int:
        """注入字符预算：优先 ``max_prompt_chars``；否则兼容旧 ``max_prompt_tokens × 2``。"""
        chars = int(getattr(self._config, "max_prompt_chars", 0) or 0)
        if chars > 0:
            return chars
        return max(1, int(self._config.max_prompt_tokens) * _PROMPT_CHARS_PER_TOKEN)

    @property
    def backend(self) -> MemoryBackend:
        self._ensure_init()
        return self._backend

    # ========= 检索 =========

    def retrieve(self, user_input: str, logger=None,
                 confidence_threshold: float = None,
                 freshness_filter: bool = True,
                 include_superseded: bool = False,
                 context: str = None) -> str:
        """检索相关记忆，返回可直接注入 prompt 的格式化文本块。

        Args:
            user_input: 用户查询文本
            logger: 日志器
            confidence_threshold: v2 置信度门槛，低于此值的记忆不注入 prompt。
                                  默认取 config.confidence_threshold（0.4）。
            context: 方案A（记忆拼接历史）—— session 历史 + 画像主题拼成的文本，
                     传给 backend 做「当前问题 + 历史上下文」两路 RRF 融合召回。
        """
        _l = _as_std_logger(logger)
        if confidence_threshold is None:
            confidence_threshold = self._config.confidence_threshold
        try:
            t0 = time.time()
            self._ensure_init()
            # 把 confidence_threshold / context 传给 backend search（如果支持）
            search_kwargs = {"top_k": self._config.retrieval_top_k}
            if hasattr(self._backend, 'search'):
                import inspect
                sig = inspect.signature(self._backend.search)
                if 'confidence_threshold' in sig.parameters:
                    search_kwargs["confidence_threshold"] = confidence_threshold
                if context and 'context' in sig.parameters:
                    search_kwargs["context"] = context
            # R2：query 实体归一（代词/追问场景从会话上下文补齐股票实体）
            search_query = _augment_query_with_subject(user_input, context)
            results = self._backend.search(search_query, **search_kwargs)
            elapsed_ms = int((time.time() - t0) * 1000)

            # 召回门槛已在 backend.search() 内按各自量纲处理（FTS5=BM25 分，
            # hybrid=RRF 融合 + rerank + top_k），此处不再对 score 做硬过滤，
            # 否则 RRF 极小量纲会把相关记忆全砍成空。置信度仅作展示/次级排序。

            # v2 M2: 按 freshness 过滤/降权
            if freshness_filter:
                fm = FreshnessManager()
                results = fm.filter_stale(results, mode="exclude")

            # v2 M3: 默认过滤已被取代的记忆
            if not include_superseded:
                results = [r for r in results if not getattr(r, "superseded_by", "")]

            # 低置信度记忆默认不注入 prompt（confidence < 0.4），
            # 避免大量 🔴低信 残片污染上下文。confidence=0.5 为未知（旧数据），放行。
            if confidence_threshold > 0:
                results = [r for r in results
                           if getattr(r, "confidence", 0.5) >= confidence_threshold]

            if not results:
                _l.debug("[retrieve] no results query=%r elapsed=%dms", user_input[:60], elapsed_ms)
                return ""

            lines = ["[相关记忆片段]"]
            # 按 chunk_group_id 去重合并：同一报告的多个 chunk 合并展示。
            # 旧数据无 chunk_group_id 时按「日期|问题」兜底分组，避免同源片段重复占位。
            groups: dict = {}
            for r in results:
                gid = r.metadata.get("chunk_group_id") or (
                    f"{(r.metadata.get('date') or '')[:10]}|{r.metadata.get('source_query', '')}"
                )
                if gid not in groups:
                    groups[gid] = {"entry": r, "chunks": []}
                groups[gid]["chunks"].append(r)

            # 注入预算：优先 MEMORY_MAX_PROMPT_CHARS；否则兼容旧 max_prompt_tokens×2
            max_chars = self._resolve_max_prompt_chars()
            per_group_n = max(1, int(getattr(self._config, "max_chunks_per_group", 6) or 6))
            chunk_limit = max(1, int(getattr(self._config, "chunk_max_chars", 1000) or 1000))
            used = len(lines[0])

            for gid, gdata in groups.items():
                r = gdata["entry"]
                date_str = r.metadata.get("date", "")[:19] if r.metadata.get("date") else "未知时间"
                source = r.metadata.get("source_query", "") or "未知问题"
                subject = f"{r.stock_name} ({r.stock_code})" if r.stock_code else (r.stock_name or "—")
                # 组内按相关性取前 N 块；若 chunk_index 可用则按文档序展示（可读性）
                picked = sorted(
                    gdata["chunks"][:per_group_n],
                    key=lambda x: x.metadata.get("chunk_index", 0),
                )
                content = "\n  ".join(
                    _truncate_at_boundary(c.content, chunk_limit) for c in picked
                ).strip()
                # 旧版本写入的脏数据可能正文为空（只剩 tags）→ 整组跳过，不占 prompt
                if not content:
                    continue
                # v2: 信誉分标签
                conf_tag = ""
                if r.confidence > 0:
                    if r.confidence >= 0.8:
                        conf_tag = " 🟢高信"
                    elif r.confidence >= 0.5:
                        conf_tag = " 🟡中信"
                    else:
                        conf_tag = " 🔴低信"
                block = (
                    f"[{date_str}]{conf_tag}\n"
                    f"用户输入:「{source}」\n"
                    f"分析标的: {subject}\n"
                    f"AI分析: {content}"
                )
                if r.metadata.get("sentiment"):
                    sentiment_map = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
                    block += f" [{sentiment_map.get(r.metadata['sentiment'], '')}]"
                if r.metadata.get("tags"):
                    block += f" [{', '.join(r.metadata['tags'][:5])}]"
                # 预算耗尽：截断本块到剩余预算后停止（靠后的组按相关性已排序，优先保留靠前）
                if used + len(block) > max_chars:
                    remain = max_chars - used - 1  # 预留 1 字符分隔符，严格不超预算
                    if remain > 100:
                        lines.append(block[:remain])
                    break
                lines.append(block)
                used += len(block) + 1
            text = "\n".join(lines)
            scores = [f"{r.entry_id[:8]}={r.score:.2f}" for r in results[:5]]
            _l.info(
                "[retrieve] hits=%d backend=%s scores=[%s] elapsed=%dms chars=%d/%d query=%r",
                len(results), self._backend.name(), " ".join(scores), elapsed_ms, len(text), max_chars, user_input[:60],
            )
            return text
        except Exception:
            _l.exception("[retrieve] FAIL")
            return ""

    # ========= 归档 =========

    def archive(self, user_input: str, result: str,
                exec_state=None, logger=None) -> None:
        """从 agent 结果中提取关键信息，写入语义记忆 + 情景记忆。

        元数据来源优先级:
          1. ContextVar 硬数据 (工具执行阶段): PE/sector/stock_code
          2. ContextVar 语义数据 (仪表盘阶段): tags/key_findings/sentiment
          3. 报告文本兜底 (无仪表盘时): 正则提取
        """
        _l = logger or _log
        try:
            t0 = time.time()
            self._ensure_init()
            result_str = result if isinstance(result, str) else str(result or "")
            result_short = result_str if result_str else user_input

            # ── 从 ContextVar 聚合元数据 ──
            collected = {}
            try:
                collected = collect_metadata(user_input, result_short)
            except Exception:
                pass

            # 股票: 优先 ContextVar, 回退 exec_state
            stock_code = collected.get("stock_code", "")
            stock_name = collected.get("stock_name", "")
            if not stock_code:
                stocks = self._extract_stocks_from_state(exec_state)
                if stocks:
                    stock_code = stocks[0][0]
                    stock_name = stocks[0][1]
            stock_codes = [stock_code] if stock_code else []

            # ── 过滤无意义条目: 无股票/行业/标签/主题 → 跳过 ──
            has_stock = bool(stock_code)
            has_sector = bool(collected.get("sector_l2") or collected.get("sector_l1"))
            has_tags = bool(collected.get("tags"))
            subject_kind = collected.get("subject_kind", "unknown")
            has_subject = subject_kind in ("stock", "sector", "market")
            if not (has_stock or has_sector or has_tags or has_subject):
                _l.debug("[archive] SKIP: no stock/sector/tags/subject")
                return

            # ── 构建 metadata ──
            metadata = {
                "source_query": collected.get("source_query", user_input),
                "date": datetime.now().isoformat(),
                "tags": collected.get("tags", []),
                "subject_kind": subject_kind,
            }
            for field in ("sector_l1", "sector_l2", "pe", "pb",
                         "pe_dynamic", "change_pct", "turnover_rate",
                         "total_mv", "float_mv", "sentiment"):
                if field in collected:
                    metadata[field] = collected[field]

            # ── v2: 来源血统 + 置信度 ──
            reasoning_summary = result_short[:500] if result_short else ""
            provenance = ProvenanceBuilder.build(
                exec_state=exec_state,
                dash_meta=collected,
                reasoning_summary=reasoning_summary,
            )

            # 检测「数据不足」失败残片 -> 写入 provenance.incomplete（零迁移）。
            # 检索时据此排除，防止失败残片被当作高权重记忆召回（自指循环）。
            if _is_incomplete_result(result_short):
                if isinstance(provenance, dict):
                    provenance["incomplete"] = True
                else:
                    provenance = {"incomplete": True}

            # 构建 ConfidenceCalculator 所需上下文
            calc = ConfidenceCalculator()
            # 提取工具数据
            extracts = collected.get("_tool_extracts", []) or []
            dash_meta_for_conf = collected if collected.get("key_findings") or collected.get("sentiment") else None

            # v2: 多源印证 — 查近期同 stock + 同 sentiment 结论数
            recent_conclusions = []
            sentiment = collected.get("sentiment", "")
            if stock_code and sentiment:
                try:
                    fts5 = getattr(self._backend, '_fts5', None) or self._backend
                    if hasattr(fts5, 'get_recent_same_conclusions'):
                        recent_conclusions = fts5.get_recent_same_conclusions(
                            stock_code, sentiment, days=7
                        )
                except Exception:
                    pass

            # 构造临时 entry 以计算 freshness（基于当前日期）
            temp_entry = MemoryEntry(
                entry_id="", stock_code=stock_code, stock_name=stock_name,
                content="", metadata=metadata,
            )
            conf_result = calc.compute(
                entry=temp_entry,
                extracts=extracts,
                dash_meta=dash_meta_for_conf,
                recent_conclusions=recent_conclusions,
            )
            confidence = conf_result["confidence"]
            confidence_factors = conf_result["factors"]

            # ── v2 M2: 时效性分类 ──
            fm = FreshnessManager()
            memory_category = fm.classify(
                entry=temp_entry,
                extracts=extracts,
            )
            ttl_days = fm.compute_ttl(memory_category)
            expires_at = fm.compute_expires_at(
                metadata.get("date", datetime.now().isoformat()), ttl_days
            )

            # ── 内容: 去装饰线保留完整正文，dashboard JSON / CHART 占位符不进向量，
            #    切分后逐段存储 ──
            summary = strip_report_decor(result_short) if result_short else ""
            summary = dashboard_to_text(summary)  # JSON 噪音不进向量（核心结论等提炼为文本）
            summary = strip_chart_markers(summary)  # @@CHART:uuid@@ 无检索价值
            chunks = chunk_report(summary)
            chunk_group_id = uuid.uuid4().hex[:8]
            entries = []
            for i, chunk_text in enumerate(chunks):
                chunk_meta = dict(metadata)
                chunk_meta["chunk_index"] = i
                chunk_meta["chunk_count"] = len(chunks)
                chunk_meta["chunk_group_id"] = chunk_group_id
                entries.append(MemoryEntry(
                    entry_id="",
                    stock_code=stock_code,
                    stock_name=stock_name,
                    content=chunk_text,
                    metadata=chunk_meta,
                    confidence=confidence,
                    confidence_factors=confidence_factors,
                    provenance=provenance,
                    memory_category=memory_category,
                    ttl_days=ttl_days,
                    expires_at=expires_at,
                ))

            # 合并判定必须基于写入前快照，同批新 chunks 不属于“旧记忆”。
            merger = Merger()
            same_subject_olds = []
            conflicts = []
            if stock_code and entries:
                try:
                    old_entries = self._backend.get_by_stock(stock_code, limit=20)
                    same_subject_olds = [
                        entry for entry in old_entries
                        if entry.entry_id != entries[0].entry_id
                        and merger._is_same_subject(entries[0], entry)
                        and not getattr(entry, "superseded_by", "")
                    ]
                    if same_subject_olds:
                        conflicts = merger.detect_conflict(entries[0], same_subject_olds)
                except Exception:
                    _l.debug("[archive] merge snapshot skipped", exc_info=True)

            t_write = time.time()
            if len(entries) == 1:
                self._backend.add(entries[0])
            else:
                self._backend.add_batch(entries)
            add_ms = int((time.time() - t_write) * 1000)

            # ── v2 M3: 合并/冲突消解 ──
            if stock_code and entries and same_subject_olds:
                try:
                    new_eid = entries[0].entry_id
                    merger.mark_superseded(
                        new_eid, same_subject_olds,
                        reason="new_analysis",
                        db_path=self._config.memory_db_path,
                    )
                    old_ids = [entry.entry_id for entry in same_subject_olds]
                    self._backend.mark_superseded(
                        old_ids, new_eid, reason="new_analysis"
                    )
                    if conflicts:
                        _l.info(
                            "[archive] merge: superseded=%d conflicts=%d stock=%s",
                            len(old_ids), len(conflicts), stock_code,
                        )
                except Exception:
                    _l.debug("[archive] merge skipped", exc_info=True)

            # ── 情景记忆 ──
            try:
                epi = EpisodicMemory(self._config.memory_db_path)
                epi.log(
                    query=user_input,
                    conclusion=result_short[:800],
                    topics=collected.get("topics"),
                    key_findings=collected.get("key_findings"),
                    stocks_mentioned=stock_codes,
                    tags=collected.get("tags"),
                    session_id=current_session_id.get() or None,
                    dialog_uuid=current_dialog_uuid.get() or None,
                    trace_run_id=current_trace_run_id.get() or None,
                    confidence=confidence,  # v2
                )
            except Exception:
                _l.warning("[archive] episodic write failed", exc_info=True)

            # ── 清理 ContextVar ──
            try:
                _reset_metadata()
            except Exception:
                pass

            elapsed_ms = int((time.time() - t0) * 1000)
            _l.info(
                f"[archive] OK stock={stock_name}({stock_code})" if stock_code else "[archive] OK stock=-",
                f"tags={collected.get('tags', [])[:3]} add={add_ms}ms total={elapsed_ms}ms backend={self._backend.name()} count={self._backend.count()}",
            )

        except Exception:
            _l.exception("[archive] FAIL")

    @staticmethod
    def _extract_stocks_from_state(exec_state) -> List[tuple]:
        """从 ExecutionState 提取涉及的股票代码和名称"""
        stocks = []
        try:
            if exec_state is None:
                return stocks
            seen = set()
            for tc in getattr(exec_state, "tool_calls", []) or []:
                tool_input = getattr(tc, "tool_input", {}) or {}
                if not isinstance(tool_input, dict):
                    continue
                code = tool_input.get("stock_code") or tool_input.get("code") or tool_input.get("symbol", "")
                name = tool_input.get("stock_name") or tool_input.get("name", "")
                code = str(code).strip().strip('"').strip("'")
                name = str(name).strip().strip('"').strip("'")
                if code and code not in seen:
                    seen.add(code)
                    stocks.append((code, name if name else code))
        except Exception:
            pass
        return stocks

    # ========= 管理 =========

    def get_stats(self) -> dict:
        self._ensure_init()
        return self._backend.stats()

    def repair(self, logger=None) -> dict:
        """修复历史语义记忆：按 source_query 重新归类主题，修正 stock_name/code/sector，
        并重新写入 chroma（补齐此前因 embedding 服务未就绪而缺失的向量关联）。

        Returns:
            dict: {"checked": int, "repaired": int}
        """
        _l = logger or _log
        self._ensure_init()
        if not hasattr(self._backend, "get_all_raw"):
            return {"checked": 0, "repaired": 0}
        rows = self._backend.get_all_raw()
        repaired = 0
        checked = 0
        for r in rows:
            checked += 1
            raw_content = r.get("raw_content") or ""
            # 存量内容清洗：移除 @@CHART:uuid@@ 等无检索价值的装饰符
            cleaned_content = strip_chart_markers(raw_content)
            subj = classify_query_subject(r.get("source_query", ""))
            if subj["subject_kind"] in ("stock", "sector", "market"):
                stock_code = subj["stock_code"]
                stock_name = subj["stock_name"]
                sector_l2 = subj["sector_l2"] or r.get("sector_l2", "")
            else:
                stock_code = r.get("stock_code", "") or ""
                stock_name = r.get("stock_name", "") or ""
                sector_l2 = r.get("sector_l2", "") or ""
            if (stock_code == (r.get("stock_code") or "")
                    and stock_name == (r.get("stock_name") or "")
                    and cleaned_content == raw_content):
                continue
            meta = {
                "source_query": r.get("source_query", ""),
                "date": r.get("date") or datetime.now().isoformat(),
                "tags": r.get("tags") or [],
                "sector_l1": r.get("sector_l1") or "",
                "sector_l2": sector_l2,
                "pe": r.get("pe"), "roe": r.get("roe"),
                "volatility": r.get("volatility") or "",
                "sentiment": r.get("sentiment") or "",
            }
            entry = MemoryEntry(
                entry_id=r["entry_id"], stock_code=stock_code,
                stock_name=stock_name, content=cleaned_content,
                metadata=meta,
                confidence=r.get("confidence", 0.5),
                provenance=r.get("provenance", {}),
            )
            self._backend.add(entry)  # FTS5 REPLACE + chroma upsert（重嵌）
            repaired += 1
            _l.info("[repair] fixed eid=%s %s -> %s(%s)",
                    str(r["entry_id"])[:12], r.get("stock_name"), stock_name, stock_code)
        # 顺带回填 chunk 元数据（幂等；旧数据此前未持久化，见 2026-09-23 修复）
        self.backfill_chunk_metadata(logger=_l, force=False)
        return {"checked": checked, "repaired": repaired}

    def backfill_chunk_metadata(self, logger=None, force: bool = False) -> dict:
        """为存量语义记忆回填 chunk_index / chunk_count / chunk_group_id。

        背景（2026-09-23）：``archive`` 一直把这三字段写进 ``MemoryEntry.metadata``，
        但 FTS5 / Chroma 后端此前不持久化 metadata，导致 ``retrieve`` 的「同报告分组合并 +
        文档序」失效（同源片段错位、只取到低价值块）。新写入已落库；本方法回填旧数据。

        口径：按 ``(date[:10], source_query)`` 分组（与 ``retrieve`` 兜底口径一致），组内按
        ``rowid`` 升序（≈ 当年 ``add_batch`` 插入顺序 = 文档顺序）赋 ``chunk_index``；
        ``chunk_group_id`` 取 ``sha1(date|source_query)[:8]``，稳定可重复。

        幂等：已有 ``chunk_group_id`` 的行默认跳过；``force=True`` 全量覆盖。

        Returns:
            dict: ``{"checked": int, "updated": int}``
        """
        _l = logger or _log
        self._ensure_init()
        if not hasattr(self._backend, "get_all_raw"):
            return {"checked": 0, "updated": 0}
        rows = self._backend.get_all_raw(limit=10_000_000)
        groups: dict = {}
        for r in rows:
            date = r.get("date") or ""
            sq = r.get("source_query") or ""
            groups.setdefault(f"{date[:10]}|{sq}", []).append(r)

        records = []
        for key, items in groups.items():
            items.sort(key=lambda r: r.get("rowid", 0))
            n = len(items)
            gid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
            for i, r in enumerate(items):
                if not force and (r.get("chunk_group_id") or ""):
                    continue
                records.append((i, n, gid, r["entry_id"]))

        updated = 0
        if records:
            if hasattr(self._backend, "backfill_chunk_columns"):
                updated = self._backend.backfill_chunk_columns(records)
            elif hasattr(self._backend, "update_chunk_metadata"):
                updated = self._backend.update_chunk_metadata(records)
        _l.info("[backfill_chunks] checked=%d groups=%d updated=%d",
                len(rows), len(groups), updated)
        return {"checked": len(rows), "updated": updated}

    def search(self, query: str, top_k: int = 20,
               memory_type: str = "all") -> List[dict]:
        self._ensure_init()
        if hasattr(self._backend, "search_all"):
            return self._backend.search_all(query, top_k=top_k, memory_type=memory_type)
        results = self._backend.search(query, top_k=top_k)
        return [
            {
                "type": "semantic",
                "entry_id": r.entry_id,
                "stock_code": r.stock_code,
                "stock_name": r.stock_name,
                "content": r.content[:200] if r.content else "",
                "date": r.metadata.get("date", ""),
                "score": r.score,
                "tags": r.metadata.get("tags", []),
            }
            for r in results
        ]

    def get_by_stock(self, stock_code: str, limit: int = 50) -> List[dict]:
        self._ensure_init()
        results = []
        if hasattr(self._backend, "get_by_stock"):
            results = self._backend.get_by_stock(stock_code, limit=limit)
        return [
            {"entry_id": r.entry_id, "stock_code": r.stock_code,
             "stock_name": r.stock_name, "content": r.content}
            for r in results
        ]

    # ── v2 M3：结论演进 ──────────────────────────────────

    def get_conclusion_history(self, stock_code: str) -> dict:
        """返回某标的的完整结论演进链。

        Returns:
            {"stock_code": str, "current": [...], "history": [...], "chains": [...]}
              current: 未被 superseded 的当前结论
              history: 已被取代的历史结论
              chains: 每条演进链（旧→新）
        """
        self._ensure_init()
        try:
            entries = self._backend.get_by_stock(stock_code, limit=100)
            current = []
            history = []
            for e in entries:
                if getattr(e, "superseded_by", ""):
                    history.append({
                        "entry_id": e.entry_id,
                        "date": e.metadata.get("date", "") if e.metadata else "",
                        "content": (e.content or "")[:200],
                        "sentiment": e.metadata.get("sentiment", "") if e.metadata else "",
                        "confidence": e.confidence,
                        "superseded_by": e.superseded_by,
                    })
                else:
                    current.append({
                        "entry_id": e.entry_id,
                        "date": e.metadata.get("date", "") if e.metadata else "",
                        "content": (e.content or "")[:200],
                        "sentiment": e.metadata.get("sentiment", "") if e.metadata else "",
                        "confidence": e.confidence,
                        "memory_category": e.memory_category,
                    })

            # 构建演进链
            from memory.merge import Merger
            m = Merger()
            chains = []
            for h in history:
                chain = m.find_superseded_chain(h["entry_id"], entries)
                if chain:
                    chains.append({
                        "start_entry_id": chain[0].entry_id,
                        "length": len(chain),
                        "entries": [
                            {
                                "entry_id": c.entry_id,
                                "date": c.metadata.get("date", "") if c.metadata else "",
                                "sentiment": c.metadata.get("sentiment", "") if c.metadata else "",
                            }
                            for c in chain
                        ],
                    })

            return {
                "stock_code": stock_code,
                "current": current,
                "history": history,
                "chains": chains[:5],  # 最多 5 条演进链
            }
        except Exception:
            _log.exception("[get_conclusion_history] failed")
            return {"stock_code": stock_code, "current": [], "history": [], "chains": []}

    # ── v2 M1 收尾：来源血统查询 ─────────────────────────

    def get_provenance(self, entry_id: str) -> Optional[dict]:
        """返回某条记忆的完整来源血统 + trace 反查结果。

        供前端"为什么这么判断"按钮调用。
        """
        self._ensure_init()
        try:
            from memory.provenance import ProvenanceQuerier
            # 从 backend 取条目
            backend = self._backend
            if hasattr(backend, 'get_all_raw'):
                rows = backend.get_all_raw(limit=10000)
                for r in rows:
                    if r.get('entry_id') == entry_id:
                        prov = r.get('provenance', {})
                        if isinstance(prov, str):
                            import json as _json
                            try:
                                prov = _json.loads(prov)
                            except Exception:
                                prov = {}
                        result = {
                            "entry_id": entry_id,
                            "stock_code": r.get("stock_code", ""),
                            "stock_name": r.get("stock_name", ""),
                            "date": r.get("date", ""),
                            "confidence": r.get("confidence", 0.5),
                            "memory_category": r.get("memory_category", "general"),
                            "expires_at": r.get("expires_at", ""),
                            "provenance": prov,
                            "full_chain": None,
                        }
                        # trace 反查
                        trace_run_id = prov.get("trace_run_id", "") if isinstance(prov, dict) else ""
                        if trace_run_id:
                            try:
                                querier = ProvenanceQuerier()
                                chain = querier.get_decision_chain(trace_run_id)
                                result["full_chain"] = chain
                            except Exception:
                                pass
                        return result
            return {"entry_id": entry_id, "error": "条目不存在或不可溯源"}
        except Exception:
            _log.exception("[get_provenance] failed")
            return {"entry_id": entry_id, "error": "查询失败"}

    def reset_all(self) -> dict:
        """清空全部记忆：语义记忆 + 情景记忆 + 向量数据 + 用户画像。

        Returns:
            dict: {"fts5_deleted": int, "embedding_deleted": int, "episodic_deleted": int, "profile_reset": bool}
        """
        self._ensure_init()
        result = {"fts5_deleted": 0, "embedding_deleted": 0, "episodic_deleted": 0, "profile_reset": False}

        # 1. FTS5 语义记忆
        fts5_count = self._backend.count()
        try:
            if hasattr(self._backend, '_fts5'):
                # Hybrid: 清理底层 FTS5
                conn = self._backend._fts5._get_conn()
                conn.execute("DELETE FROM semantic_memory_fts")
                conn.execute("DELETE FROM semantic_memory_meta")
                conn.execute("DELETE FROM episode_log")
                conn.commit()
                result["fts5_deleted"] = fts5_count
            elif hasattr(self._backend, '_get_conn'):
                # 纯 FTS5
                conn = self._backend._get_conn()
                conn.execute("DELETE FROM semantic_memory_fts")
                conn.execute("DELETE FROM semantic_memory_meta")
                conn.execute("DELETE FROM episode_log")
                conn.commit()
                result["fts5_deleted"] = fts5_count
        except Exception:
            _log.exception("[reset] FTS5 cleanup failed")

        # 2. ChromaDB 向量 — 取全部 ID 再删
        if hasattr(self._backend, '_embedding') and self._backend._embedding.is_available():
            try:
                emb_count = self._backend._embedding.count()
                if emb_count > 0:
                    all_ids = self._backend._embedding._collection.get(include=[])["ids"]
                    if all_ids:
                        self._backend._embedding._collection.delete(ids=all_ids)
                result["embedding_deleted"] = emb_count
            except Exception:
                _log.exception("[reset] embedding cleanup failed")

        # 3. 用户画像
        try:
            profile_path = os.path.join(get_user_profile_dir(), f"{self._config.user_id}.json")
            if os.path.exists(profile_path):
                os.remove(profile_path)
            result["profile_reset"] = True
        except Exception:
            _log.exception("[reset] profile cleanup failed")

        return result

    # ── v2 M2: 时效性管理 ──────────────────────────────────

    def validate(self, entry_id: str) -> bool:
        """手动验证某条记忆仍有效，更新 last_validated_at 并延长 TTL。

        Returns:
            bool: 是否成功
        """
        self._ensure_init()
        try:
            now = datetime.now().isoformat()
            # 通过 FTS5 更新（底层直接操作）
            fts5 = getattr(self._backend, '_fts5', None) or self._backend
            conn = fts5._get_conn()
            conn.execute(
                "UPDATE semantic_memory_meta SET last_validated_at=? WHERE entry_id=?",
                (now, entry_id)
            )
            # 延长 TTL：重新计算 expires_at
            row = conn.execute(
                "SELECT date, ttl_days FROM semantic_memory_meta WHERE entry_id=?",
                (entry_id,)
            ).fetchone()
            if row and row[1] and row[1] > 0:
                fm = FreshnessManager()
                new_expires = fm.compute_expires_at(now, row[1])
                conn.execute(
                    "UPDATE semantic_memory_meta SET expires_at=? WHERE entry_id=?",
                    (new_expires, entry_id)
                )
            conn.commit()
            return True
        except Exception:
            _log.exception("[validate] failed for entry_id=%s", entry_id)
            return False

    def clean_episodic(self, retention_days: int = 90) -> int:
        """清理超过 retention_days 天的情景记忆，返回删除条数。"""
        try:
            from datetime import datetime, timedelta
            before = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
            epi = EpisodicMemory(self._config.memory_db_path)
            return epi.clean_before(before)
        except Exception:
            return 0

    def clean_expired(self, as_of: str | None = None) -> int:
        """通过当前 backend 清理过期语义记忆。"""
        self._ensure_init()
        return self._backend.clean_expired(as_of)

    def clean(self, before: str = None, stock_code: str = None,
              dry_run: bool = True) -> dict:
        self._ensure_init()
        result = {"dry_run": dry_run, "deleted_count": 0}
        if before:
            if dry_run:
                result["deleted_count"] = self._backend.count()
            else:
                result["deleted_count"] = self._backend.clean_before(before)
        if stock_code:
            if dry_run:
                entries = self._backend.search_structured(
                    {"stock_code": stock_code}, top_k=1000
                ) if hasattr(self._backend, "search_structured") else []
                result["deleted_count"] = len(entries)
            else:
                result["deleted_count"] = self._backend.clean_by_stock(stock_code)
        return result

    def list_all(self, limit: int = 50, offset: int = 0) -> List[dict]:
        self._ensure_init()
        results = self._backend.list_all(limit=limit, offset=offset)
        return [
            {
                "entry_id": r.entry_id,
                "stock_code": r.stock_code,
                "stock_name": r.stock_name,
                "content": r.content[:200] if r.content else "",
                "date": r.metadata.get("date", ""),
                "tags": r.metadata.get("tags", []),
                "sector_l2": r.metadata.get("sector_l2", ""),
                "sentiment": r.metadata.get("sentiment", ""),
            }
            for r in results
        ]


# 模块级单例
_sdk_instance: MemorySDK | None = None


def set_sdk(sdk: MemorySDK | None) -> None:
    """显式注册进程内共享 SDK；传入 None 恢复按需创建。"""
    global _sdk_instance
    _sdk_instance = sdk


def get_sdk() -> MemorySDK:
    global _sdk_instance
    if _sdk_instance is None:
        _sdk_instance = MemorySDK(MemoryConfig(
            backend_mode=Config.STOCK_MEMORY_BACKEND,
            embedding_fn=_build_embedding_fn(),
            retrieval_top_k=Config.MEMORY_RETRIEVAL_TOP_K,
            max_prompt_tokens=Config.MEMORY_MAX_PROMPT_TOKENS,
            max_prompt_chars=getattr(Config, "MEMORY_MAX_PROMPT_CHARS", 6000),
            max_chunks_per_group=getattr(Config, "MEMORY_MAX_CHUNKS_PER_GROUP", 6),
            chunk_max_chars=getattr(Config, "MEMORY_CHUNK_MAX_CHARS", 1000),
            episodic_retention_days=Config.MEMORY_EPISODIC_RETENTION_DAYS,
            embedding_ready_timeout=Config.STOCK_MEMORY_EMBEDDING_READY_TIMEOUT,
            embedding_ready_interval=Config.STOCK_MEMORY_EMBEDDING_READY_INTERVAL,
            confidence_threshold=Config.MEMORY_CONFIDENCE_THRESHOLD,
            min_similarity=getattr(Config, "MEMORY_MIN_SIMILARITY", MIN_SIMILARITY_DEFAULT),
            similarity_margin=getattr(Config, "MEMORY_SIMILARITY_MARGIN", SIMILARITY_MARGIN_DEFAULT),
        ))
    return _sdk_instance
