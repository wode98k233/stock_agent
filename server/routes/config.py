"""配置管理路由：/api/config..."""
import logging
import os

from fastapi import APIRouter

from server.config_schema import get_config_schema, mask_sensitive, get_env_path, get_current_mode, _SENSITIVE_KEYS
from server.schemas import SaveConfigRequest, ApplyConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/config")
async def get_config():
    from config import Config
    env_path = get_env_path()
    items = []
    for schema in get_config_schema():
        key = schema["key"]
        if key == "AGENT_MODE":
            val = get_current_mode()
        else:
            try:
                val = getattr(Config, key)
                if val is None:
                    val = ""
            except Exception:
                # Config 类中没有的 key，从环境变量读取
                val = os.getenv(key, "")
        if callable(val):
            continue
        item = dict(schema)
        if item.get("sensitive"):
            item["value"] = mask_sensitive(key, val)
        else:
            item["value"] = val
        items.append(item)
    return {"items": items, "env_path": env_path}


@router.post("/api/config/diff")
async def post_config_diff(payload: SaveConfigRequest):
    from utils.config_manager import ConfigManager
    env_path = get_env_path()
    cm = ConfigManager()
    diff = cm.generate_diff(payload.changes)
    return {"diff": diff, "env_path": env_path}


@router.post("/api/config/apply")
async def apply_config(payload: ApplyConfigRequest):
    from utils.config_manager import ConfigManager
    env_path = get_env_path()
    # 敏感键（API Key 等）：接口层面拒绝通过 API 修改，
    # 只能由用户手动编辑 .env。避免密钥经接口泄露或被覆盖。
    blocked = {k: v for k, v in (payload.changes or {}).items() if k in _SENSITIVE_KEYS}
    if blocked:
        logger.warning("CONFIG: 拒绝通过接口修改敏感配置项: %s", list(blocked.keys()))
    safe_changes = {k: v for k, v in (payload.changes or {}).items() if k not in _SENSITIVE_KEYS}
    cm = ConfigManager()
    cm.write_env_changes(safe_changes, env_path)
    changed = cm.reload()
    if "CHECKPOINT_BACKEND" in changed or "CHECKPOINT_PG_URI" in changed:
        logger.info("CONFIG: Checkpoint 后端配置已变更，新任务将使用新配置")
    _reload_runtime_deps(changed)
    return {"success": True, "changed": list(changed), "blocked_sensitive": list(blocked.keys())}


@router.post("/api/config/reload")
async def reload_config():
    """手动重新加载 .env 配置并重启相关运行时组件。

    适用场景：
    - 用户直接在编辑器中修改了 .env，页面未感知
    - Memory / LLM / jieba 等配置变更后无需重启服务即可生效
    """
    from config import Config
    env_path = get_env_path()

    if not os.path.exists(env_path):
        return {"success": False, "changed": [], "message": f".env 不存在: {env_path}"}

    changed = Config.reload()
    _reload_runtime_deps(changed)

    logger.info("CONFIG: 手动重载完成, 变更 %d 项: %s", len(changed), list(changed)[:10])
    return {"success": True, "changed": list(changed), "message": f"已重新加载 {len(changed)} 项配置"}


def _reload_runtime_deps(changed: set):
    """Config.reload() 后，重置依赖旧配置值的运行时单例。

    仅重置可安全重建的组件，不涉及正在执行中的请求状态。
    """
    # ── jieba 分词开关 ──
    if "JIEBA_ENABLED" in changed:
        try:
            import memory.backend.fts5 as fts5_mod
            fts5_mod._JIEBA = None
            fts5_mod._JIEBA_DISABLED = False
            logger.info("CONFIG: jieba 状态已重置，下次检索时按新配置初始化")
        except Exception:
            pass

    # ── Memory SDK 单例（embedding/reranker/backend 配置变更时重建） ──
    _memory_keys = {
        "STOCK_MEMORY_BACKEND", "STOCK_MEMORY_EMBEDDING",
        "STOCK_MEMORY_EMBEDDING_MODEL", "STOCK_MEMORY_LOCAL_MODEL",
        "STOCK_MEMORY_API_KEY", "STOCK_MEMORY_API_BASE",
        "STOCK_MEMORY_RERANKER", "STOCK_MEMORY_RERANKER_MODEL",
        "STOCK_MEMORY_RERANKER_API_KEY", "STOCK_MEMORY_RERANKER_API_BASE",
        "STOCK_MEMORY_RERANKER_LOCAL_DIR", "STOCK_MEMORY_RERANKER_IDLE_TTL",
        "STOCK_MEMORY_RERANKER_PRELOAD",
        "MEMORY_RETRIEVAL_TOP_K", "MEMORY_MAX_PROMPT_TOKENS",
        "MEMORY_EPISODIC_RETENTION_DAYS", "MEMORY_CONFIDENCE_THRESHOLD",
        "STOCK_MEMORY_EMBEDDING_READY_TIMEOUT", "STOCK_MEMORY_EMBEDDING_READY_INTERVAL",
    }
    if changed & _memory_keys:
        try:
            import memory.sdk as sdk_mod
            sdk_mod._sdk_instance = None
            logger.info("CONFIG: Memory SDK 单例已重置，下次检索时按新配置重建")
        except Exception:
            pass

    # ── LLM 单例（API Key/Base/Model 变更时重建） ──
    _llm_keys = {
        "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_MODEL_NAME",
        "OPENAI_HEADERS", "OPENAI_DEFAULT_PARAMS", "OPENAI_REASONING_CONTENT_POLICY",
        "EXTRA_BODY", "REPORT_LLM_MODEL", "REPORT_LLM_API_KEY", "REPORT_LLM_API_BASE",
        "COMPRESS_LLM_MODEL", "COMPRESS_LLM_API_KEY", "COMPRESS_LLM_API_BASE",
        "JUDGE_LLM_MODEL", "JUDGE_LLM_API_KEY", "JUDGE_LLM_API_BASE",
    }
    if changed & _llm_keys:
        try:
            import utils.llm_factory as llm_factory
            # 重置所有 LLM 单例缓存
            if hasattr(llm_factory, "TokenCompatibleChatOpenAI"):
                llm_factory.TokenCompatibleChatOpenAI._default_instance = None
            for _cache_list in ["_report_llm", "_compress_llm", "_judge_llm"]:
                if hasattr(llm_factory, _cache_list):
                    getattr(llm_factory, _cache_list)[0] = None
            logger.info("CONFIG: LLM 单例已重置，下次请求时按新配置重建")
        except Exception:
            pass

    # ── 缓存过期时间 ──
    if changed & {"CACHE_EXPIRE_HOURS", "CACHE_STOCK_HISTORY_HOURS", "CACHE_BOARD_HOURS",
                   "CACHE_NEWS_HOURS", "CACHE_RATING_HOURS", "CACHE_FINANCIAL_HOURS"}:
        try:
            from utils.cache.policies import _CACHE_EXPIRE_POLICIES
            _CACHE_EXPIRE_POLICIES.clear()
            logger.info("CONFIG: 缓存策略已刷新")
        except Exception:
            pass
