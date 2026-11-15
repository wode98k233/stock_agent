"""Trace 长文本的内容寻址压缩（T2）。

设计：``docs/superpowers/specs/2026-10-19-trace-storage-slimming-design.md`` §7

为什么需要它
------------
T1（``payload.py``）解决的是"不该写的别写"，但**存下来的合法内容仍然重复**：
``messages`` 里同一份 system prompt 每次调用都存一遍（实测 20.1 MB 里去重后只有 3.7 MB）、
同一段工具输出在多个 run 之间反复出现。T2 只做一件事：把长文本**按内容寻址**存一次。

编码方式：**列内哨兵 + 行工厂透明解压**
--------------------------------------
- 写入：``len(text) >= MIN_CHARS`` 时，把 ``zlib`` 压缩体写进 ``blobs`` 表，列值改成
  ``"\\x01Bzlib:<hash32>"``；短文本原样内联（小值不值得多一次查表）。
- 读取：给连接装 ``row_factory``（``install_row_factory``），**所有查询在取行时透明解压**。
  这样新增读点不会忘记解码——这是本设计最重要的性质：漏改一个读点不会静默出哨兵串。

⚠️ 三条硬约束
--------------
1. **``runs.input / runs.output`` 不参与压缩**：``store.search_runs`` 用
   ``WHERE input LIKE ?`` 在 SQL 层做全文模糊搜索，压缩后 LIKE 必然失效。
   （``steps`` 没有这种 LIKE 搜索，实测 ``search_runs`` 只查 ``runs``。）
2. **哨兵必须极不可能撞真实数据**：``\\x01`` 是控制字符，正常文本不会以它开头。
3. **解压失败不许崩**：查不到 blob / zlib 报错时返回**原值**（调用方会看到哨兵串），
   同时打 warning——宁可显示得难看，也不能让 trace 读取整条链路挂掉。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import zlib
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAGIC = "\x01B"
CODEC_ZLIB = "zlib"
SEP = ":"
# 短文本内联：低于该长度压缩收益抵不过一次查表（4KB 页 + 索引随机读）
MIN_CHARS = 1024
# 解压缓存（内容寻址 ⇒ 跨库安全，可全局共享）；按总字节设上限，防大 blobs 吃满内存
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_CACHE_MAX_ENTRY = 1024 * 1024
_cache: dict[str, str] = {}
_cache_order: list[str] = []
_cache_bytes = 0
_cache_lock = threading.Lock()


# ============================================================
# 基础编解码
# ============================================================

def is_ref(value: Any) -> bool:
    """列值是否为压缩引用哨兵。"""
    return isinstance(value, str) and value.startswith(MAGIC)


def _parse_ref(value: str) -> Optional[tuple[str, str]]:
    rest = value[len(MAGIC):]
    codec, sep, digest = rest.partition(SEP)
    if not sep or not codec or not digest:
        return None
    return codec, digest


def put_blob(conn: sqlite3.Connection, text: str) -> str:
    """把文本压缩进 ``blobs``（幂等），返回哨兵。

    ``raw_len`` 记**UTF-8 字节数**（不是字符数）：它是给"压缩比/节省量"这类口径用的，
    跟文件体积必须同单位；中文 3 字节，混用会把压缩比低估一半以上。
    """
    raw = text.encode("utf-8")
    digest = _hash_hex(raw)
    existing = conn.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT OR IGNORE INTO blobs(hash,codec,raw_len,data) VALUES(?,?,?,?)",
            (digest, CODEC_ZLIB, len(raw), zlib.compress(raw, 6)),
        )
    return f"{MAGIC}{CODEC_ZLIB}{SEP}{digest}"


def get_blob(conn: sqlite3.Connection, codec: str, digest: str) -> Optional[str]:
    """按 hash 取回原文（走缓存）。"""
    cached = _cache_get(digest)
    if cached is not None:
        return cached
    try:
        row = conn.execute(
            "SELECT codec, data FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    row_codec, data = row[0], row[1]
    try:
        raw = zlib.decompress(bytes(data)) if (row_codec or codec) == CODEC_ZLIB else bytes(data)
        text = raw.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 解压失败不该拖垮 trace 读取
        logger.warning("[blobstore] 解压失败 hash=%s: %s", digest, exc)
        return None
    _cache_put(digest, text)
    return text


def encode_text(conn: sqlite3.Connection, value: Any, *, min_chars: int = MIN_CHARS) -> Any:
    """写入侧：长文本落 blob 并返回哨兵；短文本 / 非字符串原样返回。

    ⚠️ 调用方负责先把结构化对象序列化（``_json``）再进来，否则 dict 会被原样写回，
    压缩就不会生效（保守行为：绝不改变值的类型）。
    """
    if not isinstance(value, str) or len(value) < min_chars:
        return value
    try:
        return put_blob(conn, value)
    except sqlite3.Error as exc:  # 压缩只是优化，失败就退回内联
        logger.warning("[blobstore] 写入 blob 失败，回退内联: %s", exc)
        return value


def decode_text(conn: sqlite3.Connection, value: Any) -> Any:
    """读取侧：把哨兵还原成原文；非哨兵原样返回（老格式天然兼容）。"""
    if not is_ref(value):
        return value
    parsed = _parse_ref(value)  # type: ignore[arg-type]
    if parsed is None:
        return value
    codec, digest = parsed
    text = get_blob(conn, codec, digest)
    if text is None:
        logger.warning("[blobstore] 引用无对应 blob（未迁移 / 已损坏）: %s", value[:48])
        return value
    return text


def _hash_hex(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:32]


def _cache_get(digest: str) -> Optional[str]:
    if not _cache:
        return None
    with _cache_lock:
        return _cache.get(digest)


def _cache_put(digest: str, text: str) -> None:
    global _cache_bytes
    size = len(text)
    if size > _CACHE_MAX_ENTRY:
        return
    with _cache_lock:
        if digest in _cache:
            return
        while _cache_bytes + size > _CACHE_MAX_BYTES and _cache_order:
            victim = _cache_order.pop(0)
            old = _cache.pop(victim, None)
            if old is not None:
                _cache_bytes -= len(old)
        _cache[digest] = text
        _cache_order.append(digest)
        _cache_bytes += size


def clear_cache() -> None:
    """测试 / 回填后重置用。"""
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_order.clear()
        _cache_bytes = 0


# ============================================================
# 行工厂：让所有读点自动透明解压
# ============================================================

class DecodedRow:
    """``sqlite3.Row`` 的替代品：值已解压，支持 ``row["col"]`` / ``dict(row)`` / 下标。"""

    __slots__ = ("_cols", "_vals", "_idx")

    def __init__(self, cols: list[str], vals: list[Any]):
        self._cols = cols
        self._vals = vals
        self._idx = {c: i for i, c in enumerate(cols)}

    def keys(self) -> list[str]:
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._vals[self._idx[key]]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self) -> int:
        return len(self._vals)

    def __contains__(self, key) -> bool:
        return key in self._idx

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        pairs = ", ".join(f"{c}={self._vals[i]!r}" for i, c in enumerate(self._cols))
        return f"DecodedRow({pairs})"


def decoding_row_factory(cursor, row):
    """给连接用：取行时对每个值做 ``decode_text``。"""
    conn = cursor.connection
    cols = [d[0] for d in (cursor.description or ())]
    return DecodedRow(cols, [decode_text(conn, v) for v in row])


def install_row_factory(conn: sqlite3.Connection) -> sqlite3.Connection:
    """把连接切到透明解压行工厂（幂等）。"""
    conn.row_factory = decoding_row_factory
    return conn


# ============================================================
# 统计（回填脚本 / 探针用）
# ============================================================

def blob_stats(conn: sqlite3.Connection) -> dict:
    """blobs 表体量与压缩收益。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*), coalesce(sum(raw_len),0), coalesce(sum(length(data)),0) FROM blobs"
        ).fetchone()
    except sqlite3.Error:
        return {"blobs": 0, "raw_bytes": 0, "packed_bytes": 0, "ratio": 0.0}
    count, raw_bytes, packed_bytes = (row[0] or 0), (row[1] or 0), (row[2] or 0)
    return {
        "blobs": int(count),
        "raw_bytes": int(raw_bytes),
        "packed_bytes": int(packed_bytes),
        "ratio": round(raw_bytes / packed_bytes, 2) if packed_bytes else 0.0,
    }
