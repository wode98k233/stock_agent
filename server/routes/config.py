"""配置管理路由：/api/config..."""
import asyncio
import logging
import os
import time

from fastapi import APIRouter, HTTPException

from server.config_schema import (get_config_schema, get_env_path, get_current_mode,
                                  _SENSITIVE_KEYS, is_readonly)
from server.schemas import SaveConfigRequest, ApplyConfigRequest, ConfigTestRequest

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
            # 只写不读：永不返回密钥内容（连掩码都不给），只告诉页面「是否已配置」
            item["value"] = ""
            item["configured"] = bool(str(os.getenv(key, "")).strip())
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
    from utils.config_manager import ConfigManager, env_roundtrip_safe

    env_path = get_env_path()
    changes = dict(payload.changes or {})
    raw_secrets = dict(payload.secrets or {})
    # 只读项 + 敏感项一律不允许从 changes 写入：
    # 敏感项必须走 secrets，且**只接受非空值**（留空 = 保持原值，绝不把 Key 写成空）
    blocked = {k for k in changes if k in _SENSITIVE_KEYS or is_readonly(k)}
    # secrets 里塞非敏感/不存在的 key 也不写，但明确回报（静默丢弃最难查）
    blocked |= {k for k in raw_secrets if k not in _SENSITIVE_KEYS}
    safe_changes = {k: v for k, v in changes.items() if k not in blocked}
    secrets = {k: str(v).strip() for k, v in raw_secrets.items()
               if k in _SENSITIVE_KEYS and str(v or "").strip()}

    # dotenv 读不回来的值一律拒绝：`${...}` 会被变量替换、` #` 之后会被当注释截断、
    # 换行会破坏整个文件。否则表现是「保存成功但 Key 已被悄悄改掉」——比报错糟得多。
    unsafe = sorted(k for k, v in secrets.items() if not env_roundtrip_safe(v))
    if unsafe:
        raise HTTPException(
            status_code=400,
            detail="这些值无法原样写入 .env（含 ${...} 变量替换 / 换行 / 会被注释截断的字符），"
                   "请手工编辑 .env：" + "、".join(unsafe),
        )

    cm = ConfigManager()
    if safe_changes:
        cm.write_env_changes(safe_changes, env_path)
    if secrets:
        cm.write_env_changes(secrets, env_path)
    changed = cm.reload()
    if "CHECKPOINT_BACKEND" in changed or "CHECKPOINT_PG_URI" in changed:
        logger.info("CONFIG: Checkpoint 后端配置已变更，新任务将使用新配置")
    _reload_runtime_deps(changed)
    if blocked:
        logger.info("CONFIG: 忽略不可通过接口修改的配置项: %s", sorted(blocked))
    return {"success": True, "changed": list(changed), "blocked": sorted(blocked),
            "secrets_updated": sorted(secrets)}


@router.post("/api/config/test")
async def test_config(payload: ConfigTestRequest):
    """配置页「连通性检测」：llm / search / datasource / memory。

    只读探测：不写 .env、不落库；失败也返回 200 + success=False，
    方便页面把原因直接贴在按钮旁边。
    """
    kind = (payload.kind or "").strip().lower()
    if kind not in _TEST_KINDS:
        return {"success": False, "kind": kind, "latency_ms": 0,
                "message": f"未知的检测类型: {kind or '(空)'}"}

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        detail = await loop.run_in_executor(None, lambda: _run_config_test(kind, payload))
    except Exception as e:  # noqa: BLE001 —— 探测失败原因要原样回给页面
        safe_msg = _friendly_test_error(kind, e)
        logger.warning("CONFIG: 连通性检测失败 kind=%s: %s", kind, safe_msg)
        return {"success": False, "kind": kind,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "message": safe_msg}
    latency = int((time.monotonic() - started) * 1000)
    return {"success": True, "kind": kind, "latency_ms": latency,
            "message": detail.pop("message", "连通"), "detail": detail}


_TEST_KINDS = ("llm", "search", "datasource", "memory")

# LLM 用途 → 取实例的函数名（延迟 import，避免路由加载时就拉起 LLM 依赖）
_LLM_GETTERS = {
    "main": "get_llm",
    "report": "get_report_llm",
    "compress": "get_compress_llm",
    "judge": "get_judge_llm",
}


def _run_config_test(kind: str, payload: ConfigTestRequest) -> dict:
    """同步执行一次探测（跑在线程池里，不阻塞事件循环）。失败抛异常。"""
    if kind == "llm":
        return _test_llm(payload.target)
    if kind == "search":
        return _test_search(payload.query)
    if kind == "datasource":
        return _test_datasource()
    return _test_memory()


def _test_llm(target: str) -> dict:
    from utils import llm_factory

    purpose = (target or "main").strip().lower()
    fn_name = _LLM_GETTERS.get(purpose)
    if fn_name is None:
        raise ValueError(f"未知的 LLM 用途: {target or '(空)'}，可选 {sorted(_LLM_GETTERS)}")
    llm = getattr(llm_factory, fn_name)()

    try:
        resp = llm.invoke("连通性检测，请只回复：pong")
    except Exception as e:  # noqa: BLE001 —— 归类后交给上层拼可读文案
        raise RuntimeError(f"[{llm_factory.classify_llm_error(e)}] {e}") from e

    content = getattr(resp, "content", "")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    model = getattr(llm, "model_name", "") or getattr(llm, "model", "") or ""
    return {
        "message": f"{purpose} 模型连通（{model or '未知模型'}）",
        "purpose": purpose,
        "model": model,
        "reply": str(content).strip()[:80],
    }


def _test_search(query: str) -> dict:
    from tools.search.service import list_available_providers, run_web_search

    providers = list_available_providers()
    payload = run_web_search(query or "贵州茅台 最新公告", max_results=3, timeout=45)
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "所有搜索引擎均无结果")

    results = payload.get("results") or []
    first = results[0] if results else {}
    return {
        "message": f"搜索连通，返回 {len(results)} 条结果",
        "providers": providers,
        "provider": payload.get("provider", ""),
        "result_count": len(results),
        "first_title": str(first.get("title", "") or "")[:60],
    }


def _test_datasource() -> dict:
    from config import Config
    from tools.stock_data import get_stock_realtime

    data = get_stock_realtime("600519") or {}
    price = data.get("price")
    if not price:
        raise RuntimeError("实时行情返回空价格（数据源可能全部失败）")
    return {
        "message": f"数据源连通：{data.get('name') or '600519'} 最新价 {price}",
        "code": data.get("code", "600519"),
        "name": data.get("name", ""),
        "price": price,
        "pct_chg": data.get("pct_chg"),
        "priority_order": getattr(Config, "DATASOURCE_PRIORITY_ORDER", "") or "",
    }


def _test_memory() -> dict:
    from memory.sdk import get_sdk

    stats = get_sdk().get_stats() or {}
    keys = ("backend", "backend_mode", "semantic_count", "episodic_count", "total", "count")
    brief = {k: stats[k] for k in keys if k in stats}
    return {
        "message": f"记忆后端连通（{stats.get('backend') or stats.get('backend_mode') or 'auto'}）",
        "stats": brief,
    }


def _redact_secrets(text: str) -> str:
    """把文本里可能夹带的密钥明文替换掉（LLM 报错常回显 key 片段）。"""
    from config import Config

    s = str(text or "")
    for key in _SENSITIVE_KEYS:
        for raw in (os.getenv(key) or "", getattr(Config, key, "") or ""):
            val = str(raw).strip()
            if len(val) >= 6 and val in s:
                s = s.replace(val, "***")
    return s


def _friendly_test_error(kind: str, exc: Exception) -> str:
    """把探测异常翻成一句可操作的中文提示（密钥先脱敏）。"""
    msg = _redact_secrets(str(exc).strip() or exc.__class__.__name__)
    if len(msg) > 300:
        msg = msg[:300] + "…"
    if kind == "llm" and msg.startswith("["):
        code = msg[1:msg.find("]")] if "]" in msg else ""
        hints = {
            "auth": "API Key 无效或未配置",
            "quota": "账户额度不足",
            "rate_limit": "触发限流，稍后重试",
            "network": "网络/服务端不可达",
        }
        if code in hints:
            return f"{hints[code]}（{code}）：{msg}"
    if kind == "search":
        return f"搜索不可用：{msg}"
    if kind == "datasource":
        return f"数据源不可用：{msg}"
    if kind == "memory":
        return f"记忆后端不可用：{msg}"
    return msg


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
        "TOOL_COMPRESS_LLM_MODEL", "TOOL_COMPRESS_LLM_API_KEY", "TOOL_COMPRESS_LLM_API_BASE",
        "JUDGE_LLM_MODEL", "JUDGE_LLM_API_KEY", "JUDGE_LLM_API_BASE",
    }
    if changed & _llm_keys:
        try:
            import utils.llm_factory as llm_factory
            # 主 LLM 单例：get_llm() → LLMFactory.get_default()，缓存挂在 LLMFactory 类，
            # 重置必须打在 LLMFactory 上。（历史 bug：误重置
            # TokenCompatibleChatOpenAI._default_instance —— 该类无此缓存，
            # 导致 OPENAI_MODEL_NAME / API_BASE 等主模型配置重载后不生效。）
            llm_factory.LLMFactory._default_instance = None
            # 具名 LLM 缓存：_report_llm / _compress_llm / _judge_llm（[实例, 锁] 列表）
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
