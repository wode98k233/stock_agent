"""标的别名查询（读 market_data.db.symbol_alias）。

红线：名称→代码的映射是**数据**，不是代码。本模块只查询，不内置任何别名。
数据源 `data/symbol_aliases.json`（运营直接改这个文件）→
`scripts/ops/seed_symbol_aliases.py --apply` 灌库（幂等 upsert）。

⚠️ **缺库/空表自动补种**（2026-11-01 P1）：`db/` 不进 git，新环境/新容器如果不跑
seed 脚本，别名就查不到 → 名称被当成代码（真机复现：`跑沪深300` 落成
`POOL:codes:000300`，指数被当个股回测，**静默换标的**）。所以任何查询在发现
「表不存在 / 表为空」时会先从 `data/symbol_aliases.json` 自动建表+灌一次（幂等，
每个库路径每进程只做一次）；连补种都失败才告警并**明确查不到**（调用方另有防线，
见 `backtest.pool_scope` 的指数码拒绝）。

kind 语义（同一张表按用途分区，避免「沪深300」既是指数又是 ETF 时互相污染）：
- `index`  ：指数名 → 指数代码（范围解析用，如 沪深300 → 000300）
- `etf`    ：基金简称 → ETF 代码（ETF 识别/代码池用，如 沪深300 → 510300）
- `stock`  ：个股简称 → 代码（人话里的个股识别）
- `synonym`：一组代码的统称，同一 alias 多行（如 易中天 → 3 只光模块）

行序有意义（`data/symbol_aliases.json` 的行序 = 灌库 rowid 顺序）：
- `lookup_label` 取该代码的**第一条中文别名**作为展示名；
- `find_alias_in_text` 长别名优先、等长按行序。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "symbol_aliases.json"

logger = logging.getLogger("radar.symbol_alias")


class SymbolAliasNotReady(RuntimeError):
    """别名库不可用（缺表为空且自动补种失败）——调用方必须显式降级，不许静默猜。"""


# 每个库路径「已确认可用」的进程内缓存（避免每次查询都 COUNT）
_READY: Dict[str, bool] = {}
_READY_LOCK = threading.Lock()


def _db_path() -> str:
    try:
        from utils.app_paths import get_market_data_db_path
        return get_market_data_db_path()
    except Exception:
        return ""


def alias_db_path() -> str:
    """别名库（= market_data.db）路径；取不到返回空串（调用方按「查不到」处理）。"""
    return _db_path()


def _load_seed_rows() -> List[Dict[str, str]]:
    """读 data/symbol_aliases.json → [{alias, code, kind}]（顺序即灌库 rowid 顺序）。"""
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    rows: List[Dict[str, str]] = []
    for r in data.get("aliases") or []:
        alias = str(r.get("alias") or "").strip()
        code = str(r.get("code") or "").strip()
        if not alias or not code:
            continue
        rows.append({"alias": alias, "code": code,
                     "kind": str(r.get("kind") or "etf").strip().lower()})
    return rows


def _alias_row_count(path: str) -> int:
    """symbol_alias 行数；表不存在/库打不开返回 -1（调用方据此补建）。"""
    conn = None
    try:
        conn = sqlite3.connect(path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM symbol_alias").fetchone()[0])
        except sqlite3.Error:
            return -1
    except Exception:
        return -1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def seed_from_data_file(db_path: Optional[str] = None, *,
                        source: str = "seed") -> int:
    """把 data/symbol_aliases.json 灌进库（建表 + 幂等 upsert），返回行数。

    路径参数为空时用 market_data.db 默认路径。`source` 用来区分
    「运维显式灌库(seed)」与「查询层自动补种(auto_seed)」，便于排查。
    本函数是 `scripts/ops/seed_symbol_aliases.py` 与自动补种的**唯一写入实现**。
    """
    rows = _load_seed_rows()
    if not rows:
        raise ValueError(f"别名数据为空或格式不对: {SEED_PATH}")
    path = db_path or _db_path()
    if not path:
        raise ValueError("拿不到 market_data.db 路径（utils.app_paths）")

    from utils.db_schema.migrator import ensure_schema
    from utils.db_schema.tables import market_data as market_data_tables

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        ensure_schema(conn, market_data_tables.TABLES, [])
        for r in rows:
            conn.execute(
                "INSERT INTO symbol_alias (alias, code, kind, source, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(alias, code) DO UPDATE SET kind=excluded.kind, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (r["alias"], r["code"], r["kind"], source, now))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _ensure_ready(path: str, *, fresh: bool = False) -> bool:
    """保证别名库可用：缺表/空表就自动补种；返回是否可用。

    每个库路径每进程只判定一次（`_READY` 缓存）；`fresh=True` 强制重新判定。
    补种失败 → 告警 + False（调用方按「查不到」处理，但**不允许**因此把名称当代码）。
    """
    if not path:
        return False
    if not fresh:
        cached = _READY.get(path)
        if cached is not None:
            return cached
    with _READY_LOCK:
        if not fresh:
            cached = _READY.get(path)
            if cached is not None:
                return cached
        ok = False
        seeded = 0
        try:
            if _alias_row_count(path) <= 0:
                seeded = seed_from_data_file(path, source="auto_seed")
            ok = _alias_row_count(path) > 0
        except Exception as e:  # noqa: BLE001 - 补种失败不阻断主流程，但不许静默
            logger.warning(
                "symbol_alias 不可用（%s）：%s。别名查不到 → 名称不会被当代码解析；"
                "请跑 scripts/ops/seed_symbol_aliases.py --apply 或检查 %s",
                path, e, SEED_PATH)
            ok = _alias_row_count(path) > 0        # 别的进程可能刚灌好
        if ok and seeded:
            logger.info("symbol_alias 缺库/空表，已从 %s 自动补种 %d 行（%s）",
                        SEED_PATH.name, seeded, path)
        _READY[path] = ok
        return ok


def is_ready(db_path: Optional[str] = None) -> bool:
    """别名库是否可用（缺库/空表会先自动补种，返回补种后的结果）。"""
    return _ensure_ready(db_path or _db_path())


def require_ready(db_path: Optional[str] = None) -> None:
    """别名库自检：不可用则抛 `SymbolAliasNotReady`（给「必须拿到别名」的调用方用）。"""
    path = db_path or _db_path()
    if not _ensure_ready(path, fresh=True):
        raise SymbolAliasNotReady(
            f"别名库不可用：{path or '<空路径>'}。请跑 "
            f"scripts/ops/seed_symbol_aliases.py --apply（数据源 {SEED_PATH}）")


def _connect(db_path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    path = db_path or _db_path()
    if not _ensure_ready(path):       # 缺库/空表 → 自动补种；失败按「查不到」处理
        return None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def lookup_one(alias: str, db_path: Optional[str] = None, *,
               kind: str = "etf") -> Optional[str]:
    """单个 kind 内精确别名（大小写不敏感）→ 代码；同 alias 多行取灌库顺序第一条。"""
    key = (alias or "").strip()
    if not key:
        return None
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT code FROM symbol_alias WHERE alias=? COLLATE NOCASE "
            "AND LOWER(kind)=LOWER(?) ORDER BY rowid LIMIT 1",
            (key, str(kind))).fetchone()
        if row:
            return str(row["code"])
        # 大小写/空格宽松匹配（「CPO」vs「cpo」）
        row = conn.execute(
            "SELECT code FROM symbol_alias WHERE LOWER(alias)=LOWER(?) "
            "AND LOWER(kind)=LOWER(?) ORDER BY rowid LIMIT 1",
            (key, str(kind))).fetchone()
        return str(row["code"]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def lookup_group(alias: str, db_path: Optional[str] = None) -> List[str]:
    """一组别名（kind=synonym）→ 代码列表（保序去重）。"""
    key = (alias or "").strip()
    if not key:
        return []
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT code FROM symbol_alias WHERE LOWER(alias)=LOWER(?) "
            "AND LOWER(kind)='synonym' ORDER BY rowid",
            (key,)).fetchall()
        out: List[str] = []
        for r in rows:
            c = str(r["code"])
            if c not in out:
                out.append(c)
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def list_aliases(kind: str = "etf",
                 db_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """某个 kind 下的全部 (别名, 代码)，按灌库顺序（= data/symbol_aliases.json 的行序）。

    调用方可以在自己的进程内缓存（本函数每次都开连接）；数据量很小（几十行）。
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT alias, code FROM symbol_alias WHERE LOWER(kind)=LOWER(?) "
            "ORDER BY rowid", (str(kind),)).fetchall()
        out: List[Tuple[str, str]] = []
        for r in rows:
            name = str(r["alias"]).strip()
            if name:
                out.append((name, str(r["code"])))
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def lookup_label(code: str, db_path: Optional[str] = None, *,
                 kind: str = "index") -> str:
    """代码 → 展示名（该 kind 下第一条含中文的别名）；查不到返回空串。

    为什么按「第一条中文别名」：同一代码可能存了 `沪深300` / `沪深三百` / `hs300`
    三条，展示名只该是中文全称那条；纯数字/拉丁别名（hs300）不当展示名。
    真实展示名缺失时返回空串，调用方降级成代码本身（不编名字）。
    """
    key = (code or "").strip()
    if not key:
        return ""
    conn = _connect(db_path)
    if conn is None:
        return ""
    try:
        rows = conn.execute(
            "SELECT alias FROM symbol_alias WHERE code=? AND LOWER(kind)=LOWER(?) "
            "ORDER BY rowid", (key, str(kind))).fetchall()
        names = [str(r["alias"]).strip() for r in rows if str(r["alias"]).strip()]
        for name in names:
            if _CJK_RE.search(name):
                return name
        return names[0] if names else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def find_aliases_in_text(text: str, db_path: Optional[str] = None, *,
                         kind: str = "index") -> List[Tuple[str, str]]:
    """文本里命中的该 kind **全部**别名，长别名优先、等长按数据文件行序。

    为什么不是正则命中就算：调用方常要把「子串命中」再筛一遍（例如范围解析里
    「沪深300ETF」不该被当成指数名「沪深300」），所以给全量、排好序的列表。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    low = raw.lower()
    found: List[Tuple[Tuple[int, int], str, str]] = []
    for order, (alias, code) in enumerate(list_aliases(kind, db_path)):
        name = alias.strip()
        if not name or name.lower() not in low:
            continue
        found.append(((-len(name), order), name, code))
    found.sort(key=lambda item: item[0])
    return [(name, code) for _, name, code in found]


def find_alias_in_text(text: str, db_path: Optional[str] = None, *,
                       kind: str = "index") -> Optional[Tuple[str, str]]:
    """文本里命中的该 kind 别名 → (别名, 代码)；没命中返回 None。

    选哪一个命中：**长别名优先**（「沪深300ETF」不该被「沪深300」抢先），
    等长按数据文件行序（`沪深300` 排在 `中证500` 前，与旧实现的判定顺序一致）。
    """
    hits = find_aliases_in_text(text, db_path, kind=kind)
    return hits[0] if hits else None
