"""
选股雷达 — LLM 网关语义缓存策略（A8 T03）
==========================================

**解决什么问题**：盘中同类问题（「今天大盘怎么看」）被反复问，每次全价走完整 LLM 链。
本 policy 在网关调用链**前置**做「归一化精确匹配 + 时间桶」缓存：命中直接返回缓存响应
（<1s，零 LLM 调用），未命中放行走既有链并在响应后回填缓存。

设计裁决（详见 docs/arch/A8-语义缓存与模型路由-设计.md §1.3-1.4）：
- **存储复用** ``market_cache.db::cache_kv`` 现有表（不建新表），key 格式 ``llm_resp:v1:...``；
  expire_at 用表原生 TTL 语义，cleaners 既有清理逻辑自动生效；
- **上下文通道**：调用方经 ``config["metadata"]`` 传 ``sr_*`` 保留键（sr_scenario /
  sr_template_id / sr_inject_hash / sr_cacheable）；market_phase 由本 policy 内现算
  （``is_trading_hours('cn')``），不信任调用方传入；
- **保守原则（宁 miss 不错缓存）**：
    1. metadata 无任何 ``sr_*`` 键 → 不缓存（无法判断个性化程度）；
    2. ``sr_cacheable`` 非显式 True → 不缓存；
    3. ``sr_inject_hash`` 缺失视为有注入 → 键内取随机值，天然必 miss；
- **TTL 分级（配置化）**：市场观点 30min；板块/个股 盘中禁缓存、盘后 4h；
  方法论/知识 7d；数据查询禁缓存。市场观点与板块类**跨自然日失效**
  （``get_effective_trading_date`` 交易口径守卫，AC-4）；
- **响应上限**：单条 >200KB 不存（设计 §7.3，报告类大响应兜底）；
- 命中/未命中计数落进程内 ``CacheStats``（单条目命中数落在信封 ``hits`` 字段，
  不依赖 cache_kv 表的 hit_count 列——实测该表无此列，见汇报偏差说明）。

流式链（``_run_chat_stream_*``）**一期不接缓存**：流式响应缓存语义复杂，且缓存主目标
（报告类大响应）走的是 invoke 链。

扩展指引：
- 要调 TTL / 场景映射：改 ``LLM_CACHE_TTL_MAP`` / ``LLM_CACHE_CATEGORY_MAP`` 配置（热更生效）；
- 要换存储：改 ``_conn`` / ``lookup`` / ``store``，网关只依赖 build_key/lookup/store 契约。
"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from utils.cache.market_cache_db import get_market_cache_db
from utils.trading_calendar import get_effective_trading_date, is_trading_hours

_log = logging.getLogger("utils.llm.policies.cache_policy")

# ── 常量 ──

KEY_PREFIX = "llm_resp:"          # cache_kv 键前缀（"表名:key" 约定）
KEY_VERSION = "v1"                # 键版本号，便于将来失效策略升级
PHASE_INTRADAY = "intraday"       # 盘中（连续竞价时段）
PHASE_OFFHOURS = "offhours"       # 盘后 / 非交易时段
GUARDED_CATEGORIES = {"market", "sector"}   # 启用跨日守卫的缓存类别
DEFAULT_CATEGORY = "sector"       # 未列出场景的兜底类别
NO_TTL = 0                        # TTL=0 → 禁缓存
MAX_RESPONSE_BYTES = 200 * 1024   # 单条缓存响应上限（设计 §7.3：超出不存）

# TTL 分级默认值（秒；0=禁缓存），可用 LLM_CACHE_TTL_MAP 覆盖
DEFAULT_TTL_MAP = {
    "market": 1800,            # 市场观点类 30min
    "sector_intraday": 0,      # 板块/个股分析类：盘中禁缓存（FR-03）
    "sector_offhours": 14400,  # 板块/个股分析类：盘后 4h
    "methodology": 604800,     # 方法论/知识类 7d
    "data_query": 0,           # 数据查询类禁缓存
}

# 场景 → 缓存类别映射默认值，可用 LLM_CACHE_CATEGORY_MAP 覆盖
DEFAULT_CATEGORY_MAP = {
    "market_quick": "market",
    "market_overview": "market",
    "sector_analysis": "sector",
    "stock_analysis": "sector",
    "methodology": "methodology",
    "data_query": "data_query",
    "default": "sector",
}

# 尾部语气词/标点（循环剥除直到稳定，与设计 §1.3 normalize 规则一致）
_TAIL_PARTICLES = ("呢", "吧", "啊", "呀", "嘛", "？", "?", "。", "！", "!")

# cache_kv 表 DDL（仅当显式注入 db_path（测试/独立部署）且表不存在时兜底建表；
# 生产路径复用 market_cache.db 既有表，由 init_market_cache_tables 负责）
_CACHE_KV_DDL = """CREATE TABLE IF NOT EXISTS cache_kv (
    cache_key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expire_at TIMESTAMP
)"""

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _load_json_config(attr: str, fallback: dict) -> dict:
    """从 Config 读取 JSON 配置；解析失败回退默认值（fail-safe，不崩网关）。"""
    try:
        from config import Config
        raw = getattr(Config, attr, "")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:  # json.JSONDecodeError / ImportError / OSError 等
        _log.warning("配置 %s 解析失败，使用默认值: %s", attr, e)
    return fallback


def normalize_query(text: str) -> str:
    """查询归一化（纯函数，便于单测）：去首尾空白 → 全角转半角 → 去尾部语气词 → 压缩空白。

    同义同形（「今天大盘怎么看？ 」 vs 「今天大盘怎么看」）归一化后同键命中（FR-02）。
    """
    if not text:
        return ""
    s = str(text)
    # 全角转半角：U+3000 全角空格 + FF01–FF5E 全角 ASCII 区
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out).strip()
    # 循环剥尾部语气词/标点，直到稳定（「怎么样呢？」→「怎么样」）
    changed = True
    while changed and s:
        changed = False
        for p in _TAIL_PARTICLES:
            if s.endswith(p):
                s = s[:-1].rstrip()
                changed = True
    # 连续空白压缩为单个空格
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class CacheEntry:
    """缓存命中条目（信封解包结果）。"""

    response: str                 # 缓存的 AIMessage content
    model: str                    # 产生该响应的模型名
    tier: str                     # 产生该响应时的路由档位
    created_trade_date: str       # 写入时的有效交易日（跨日守卫用，YYYY-MM-DD）


class CacheStats:
    """进程内命中率/节省 token/路由档位计数（供 stats 接口与 A7 面板消费）。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._saved_tokens = 0
            self._by_category: dict = {}
            self._routing: dict = {}

    def record_hit(self, category: str, saved_tokens: int = 0) -> None:
        with self._lock:
            self._hits += 1
            self._saved_tokens += max(0, int(saved_tokens or 0))
            cat = self._by_category.setdefault(category, {"hits": 0, "misses": 0})
            cat["hits"] += 1

    def record_miss(self, category: str) -> None:
        with self._lock:
            self._misses += 1
            cat = self._by_category.setdefault(category, {"hits": 0, "misses": 0})
            cat["misses"] += 1

    def record_routing(self, tier: str) -> None:
        with self._lock:
            self._routing[tier] = self._routing.get(tier, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "saved_tokens_est": self._saved_tokens,
                "by_category": {k: dict(v) for k, v in self._by_category.items()},
                "routing": dict(self._routing),
            }


# 模块级共享计数器：网关 / 路由 policy / stats 接口三方同源（AC-7）
_stats = CacheStats()


def get_shared_stats() -> CacheStats:
    """获取模块级共享计数器（测试 reset / A7 面板同源读取）。"""
    return _stats


class ResponseCachePolicy:
    """LLM 响应语义缓存策略（归一化精确匹配 + TTL 分级 + 跨日守卫）。

    :param db_path: 缓存库路径；留空走 market_cache.db（生产路径），注入用于测试隔离。
    :param ttl_map: TTL 分级映射（秒，0=禁缓存）；留空每次读 Config（热更生效）。
    :param category_map: 场景→类别映射；留空每次读 Config（热更生效）。
    :param enabled: 显式开关（测试用）；留空读 Config.LLM_CACHE_ENABLED（默认 false 灰度）。
    :param trading_hours_fn: 盘中判定注入（测试用）；默认 is_trading_hours("cn")。
    :param effective_date_fn: 有效交易日注入（测试用）；默认 get_effective_trading_date("cn")。
    """

    def __init__(self, db_path: Optional[str] = None,
                 ttl_map: Optional[dict] = None,
                 category_map: Optional[dict] = None,
                 enabled: Optional[bool] = None,
                 trading_hours_fn: Optional[Callable[[], bool]] = None,
                 effective_date_fn: Optional[Callable[[], date]] = None):
        self._db_path = db_path
        self._ttl_map_override = ttl_map
        self._category_map_override = category_map
        self._enabled_override = enabled
        self._trading_hours_fn = trading_hours_fn or (lambda: is_trading_hours("cn"))
        self._effective_date_fn = effective_date_fn or (lambda: get_effective_trading_date("cn"))
        if db_path:
            # 注入独立库路径时兜底建 cache_kv（统一 schema；生产路径复用既有表）
            try:
                from utils.db_schema.migrator import ensure_schema
                from utils.db_schema.tables.market_cache import TABLES
                with self._conn() as conn:
                    ensure_schema(conn, TABLES)
                    conn.commit()
            except sqlite3.Error as e:
                _log.warning("cache_kv 兜底建表失败: %s", e)

    # ── 连接管理 ──

    @contextmanager
    def _conn(self):
        """SQLite 连接上下文：显式 db_path 直连（测试），否则复用 market_cache.db 上下文。"""
        if self._db_path:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()
        else:
            with get_market_cache_db() as conn:
                yield conn

    # ── 配置解析 ──

    @property
    def enabled(self) -> bool:
        """缓存总开关（默认 false 灰度；关闭后网关链上短路，行为与上线前完全一致，AC-6）。"""
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        try:
            from config import Config
            return bool(getattr(Config, "LLM_CACHE_ENABLED", False))
        except Exception:
            return False

    def get_ttl_map(self) -> dict:
        if self._ttl_map_override is not None:
            return dict(self._ttl_map_override)
        return _load_json_config("LLM_CACHE_TTL_MAP", DEFAULT_TTL_MAP)

    def get_category_map(self) -> dict:
        if self._category_map_override is not None:
            return dict(self._category_map_override)
        return _load_json_config("LLM_CACHE_CATEGORY_MAP", DEFAULT_CATEGORY_MAP)

    # ── 相位 / 类别 / TTL ──

    def current_phase(self) -> str:
        """当前市场相位（policy 内现算，不信任调用方传入，防穿透失效）。"""
        try:
            return PHASE_INTRADAY if bool(self._trading_hours_fn()) else PHASE_OFFHOURS
        except Exception as e:
            _log.warning("盘中判定失败，按盘后处理: %s", e)
            return PHASE_OFFHOURS

    def category_for(self, scenario: str) -> str:
        """场景 → 缓存类别；未列出场景回退 default（默认 sector，保守）。"""
        cmap = self.get_category_map()
        s = str(scenario or "unknown")
        if s in cmap:
            return str(cmap[s])
        return str(cmap.get("default", DEFAULT_CATEGORY))

    def resolve_ttl(self, category: str, phase: str) -> int:
        """类别 + 相位 → TTL 秒数；0=禁缓存。板块类按盘中/盘后取不同键。"""
        ttl_map = self.get_ttl_map()
        if category == "sector":
            key = "sector_intraday" if phase == PHASE_INTRADAY else "sector_offhours"
            try:
                return int(ttl_map.get(key, NO_TTL))
            except (TypeError, ValueError):
                return NO_TTL
        try:
            return int(ttl_map.get(category, NO_TTL))
        except (TypeError, ValueError):
            return NO_TTL

    # ── 键构造 ──

    def build_key(self, *, purpose: str, label: str, query: str,
                  metadata: Optional[dict], phase: str) -> Optional[str]:
        """构造缓存键；按保守原则判定不可缓存时返回 None。

        键格式（SHA1 截 16 hex，前缀带版本号便于将来失效策略升级）::

            llm_resp:v1:{purpose}:{label}:{scenario|unknown}:{template_id|-}:{phase}:{inject}:{sha1(normalize(query))[:16]}
        """
        if not self.enabled:
            return None
        meta = metadata or {}
        # 保守原则 1：metadata 无任何 sr_* 键 → 不缓存（上下文通道未接入）
        if not any(str(k).startswith("sr_") for k in meta):
            return None
        # 保守原则 2：sr_cacheable 非显式 True → 不缓存
        if meta.get("sr_cacheable") is not True:
            return None
        scenario = str(meta.get("sr_scenario") or "unknown")
        template_id = str(meta.get("sr_template_id") or "-")
        # 保守原则 3：注入哈希缺失视为有注入 → 取随机值使键天然必 miss（宁 miss 不错缓存）
        inject_hash = str(meta.get("sr_inject_hash") or uuid.uuid4().hex)
        # TTL=0 的类别（盘中板块 / 数据查询）直接判不可缓存
        if self.resolve_ttl(self.category_for(scenario), phase) <= 0:
            return None
        qh = hashlib.sha1(normalize_query(query or "").encode("utf-8")).hexdigest()[:16]
        return (f"{KEY_PREFIX}{KEY_VERSION}:{purpose}:{label}:{scenario}:"
                f"{template_id}:{phase}:{inject_hash}:{qh}")

    # ── 读写 ──

    def lookup(self, key: Optional[str], category: str) -> Optional[CacheEntry]:
        """查缓存；TTL 过期 / 跨日守卫失败 → 删条目按 miss 处理（AC-4）。异常按 miss fail-safe。"""
        if not key or not self.enabled:
            return None
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data, expire_at FROM cache_kv WHERE cache_key = ?", (key,)
                ).fetchone()
                if row is None:
                    _stats.record_miss(category)
                    return None
                if self._is_expired(row["expire_at"]):
                    conn.execute("DELETE FROM cache_kv WHERE cache_key = ?", (key,))
                    _stats.record_miss(category)
                    return None
                try:
                    envelope = json.loads(row["data"])
                except (json.JSONDecodeError, TypeError):
                    conn.execute("DELETE FROM cache_kv WHERE cache_key = ?", (key,))
                    _stats.record_miss(category)
                    return None
                # 跨日守卫：市场观点/板块类校验「信封交易日 == 今日有效交易日」（AC-4）
                if category in GUARDED_CATEGORIES:
                    today = str(self._effective_date_fn())
                    if str(envelope.get("created_trade_date") or "") != today:
                        conn.execute("DELETE FROM cache_kv WHERE cache_key = ?", (key,))
                        _stats.record_miss(category)
                        return None
                # 单条目命中数（持久化在信封内；进程级命中数走 CacheStats）
                envelope["hits"] = int(envelope.get("hits", 0)) + 1
                conn.execute(
                    "UPDATE cache_kv SET data = ? WHERE cache_key = ?",
                    (json.dumps(envelope, ensure_ascii=False), key),
                )
                return CacheEntry(
                    response=str(envelope.get("response", "")),
                    model=str(envelope.get("model", "")),
                    tier=str(envelope.get("tier", "standard")),
                    created_trade_date=str(envelope.get("created_trade_date", "")),
                )
        except sqlite3.Error as e:
            _log.warning("缓存读取失败（按未命中处理）: %s", e)
            return None

    def store(self, key: Optional[str], category: str, response: str, model: str,
              ttl_s: int, *, tier: str = "standard", query_head: str = "") -> bool:
        """写缓存（INSERT OR REPLACE，expire_at=now+ttl）；超上限/禁缓存/异常时静默跳过。"""
        if not key or not self.enabled:
            return False
        ttl_s = int(ttl_s or 0)
        if ttl_s <= 0:
            return False
        # 单条响应上限：>200KB 不存（设计 §7.3 开发自决项）
        if len((response or "").encode("utf-8")) > MAX_RESPONSE_BYTES:
            _log.info("缓存跳过：响应超过 %d 字节上限", MAX_RESPONSE_BYTES)
            return False
        now = datetime.now()
        envelope = {
            "response": response or "",
            "model": model or "",
            "tier": tier or "standard",
            "created_trade_date": str(self._effective_date_fn()),
            "query_head": (query_head or "")[:80],  # 排查用：query 前 80 字
            "hits": 0,
        }
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache_kv (cache_key, data, created_at, expire_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, json.dumps(envelope, ensure_ascii=False),
                     now.strftime(_TS_FMT), (now + timedelta(seconds=ttl_s)).strftime(_TS_FMT)),
                )
            return True
        except sqlite3.Error as e:
            _log.warning("缓存写入失败（忽略）: %s", e)
            return False

    # ── 计量辅助 ──

    def record_hit(self, category: str, saved_tokens: int = 0) -> None:
        """命中计数（网关在命中短路时调用，附节省 token 估算）。"""
        _stats.record_hit(category, saved_tokens)

    @staticmethod
    def estimate_saved_tokens(query_text: str, response: str) -> int:
        """命中节省 token 估算：输入 len//4 + 响应 len//4（与网关限流同口径）。"""
        return max(1, len(query_text or "") // 4) + max(1, len(response or "") // 4)

    # ── 过期判断 ──

    @staticmethod
    def _is_expired(expire_at) -> bool:
        if not expire_at:
            return False
        try:
            return datetime.now() > datetime.strptime(str(expire_at), _TS_FMT)
        except ValueError:
            return False

    # ── 运维：统计 / 清理 ──

    def stats(self) -> dict:
        """统计快照（entries=库内 llm_resp:* 条目数；命中率来自进程内计数器）。"""
        snapshot = _stats.snapshot()
        entries = 0
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM cache_kv WHERE cache_key LIKE ?",
                    (KEY_PREFIX + "%",),
                ).fetchone()
                entries = int(row["n"]) if row else 0
        except sqlite3.Error as e:
            _log.warning("缓存条目统计失败: %s", e)
        snapshot["enabled"] = self.enabled
        snapshot["entries"] = entries
        total = snapshot["hits"] + snapshot["misses"]
        snapshot["hit_rate"] = round(snapshot["hits"] / total, 4) if total else 0.0
        return snapshot

    def clear(self, prefix: str = KEY_PREFIX) -> int:
        """按前缀清缓存（管理接口用），返回清除条数。"""
        n = 0
        try:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM cache_kv WHERE cache_key LIKE ?", (prefix + "%",))
                n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as e:
            _log.warning("缓存清理失败: %s", e)
        return n


def get_module_stats() -> dict:
    """模块级统计快照（供 GET /api/llm/cache/stats 与 A7 面板同源消费，AC-7）。"""
    return ResponseCachePolicy().stats()
