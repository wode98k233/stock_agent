"""标的解析：把「银行板块」这类人话解析成技能工具能吃的代码。

红线（2026-11-01 用户指出）：**名称→代码不许写死在代码里**。
本模块只保留「查询逻辑」，数据一律来自本地 `market_data.db`：
- `index_info(code, name)`          → 指数
- `stock_board(board_code, board_name, board_type)` → 行业/概念板块
- `stock_info(code, name, ...)`     → 个股/ETF

解析顺序（成本由低到高）：
1. 本身是代码（600036 / 399986.SZ）→ 归一化；
2. 名字里带代码（「中证银行(399986.SZ)」）→ 直接取；
3. 查库（精确 → 去掉「板块/行业/概念/指数」后缀再精确 → 包含匹配，按名称长度取最接近）；
4. 可选的一次问句解析（`query_fn`，默认无，便于单测离线）；
5. 都失败 → 返回空串，调用方降级（**不猜**）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

_CODE_RE = re.compile(r"\b(\d{6})(?:\.(SZ|SH|BJ))?\b", re.I)
_SUFFIX_WORDS = ("板块", "行业", "概念", "指数", "指数板块", "主题")


def looks_like_code(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.fullmatch(r"(?:[a-zA-Z]{2})?\d{6}(?:\.(SZ|SH|BJ))?", t))


def normalize_code(code: str) -> str:
    """6 位数字补市场后缀；已带 sh/sz/大写后缀的先拆再补。"""
    t = (code or "").strip()
    m = re.fullmatch(r"(?:(sh|sz|bj)\.?)?(\d{6})(?:\.(SZ|SH|BJ))?", t, re.I)
    if not m:
        return t  # 非 6 位（如 BK1283 / gn_myyx）原样返回
    prefix, num, suf = (m.group(1) or "").lower(), m.group(2), (m.group(3) or "").upper()
    if suf:
        return f"{num}.{suf}"
    if prefix:
        return f"{num}.{ {'sh': 'SH', 'sz': 'SZ', 'bj': 'BJ'}[prefix] }".replace(" ", "")
    if num[0] in "69":
        return f"{num}.SH"
    if num[0] in "48":
        return f"{num}.BJ"
    return f"{num}.SZ"


def normalize_index_code(code: str) -> str:
    """指数代码后缀规则：000xxx 沪市、399xxx 深市、899xxx 北交所。

    与个股规则不同（000001 个股是平安银行/深市，指数是上证指数/沪市），不能混用。
    """
    t = (code or "").strip().upper()
    m = re.fullmatch(r"(\d{6})(?:\.(SZ|SH|BJ))?", t)
    if not m:
        return t
    num, suf = m.group(1), m.group(2)
    if suf:
        return f"{num}.{suf}"
    if num.startswith("399"):
        return f"{num}.SZ"
    if num.startswith("899"):
        return f"{num}.BJ"
    return f"{num}.SH"


def extract_code(payload_text: str) -> str:
    """从工具返回（JSON 文本）里挖第一个像代码的值（优先「代码」字段）。"""
    if not payload_text:
        return ""
    try:
        data = json.loads(payload_text)
    except Exception:
        m = _CODE_RE.search(payload_text)
        return normalize_code(m.group(0)) if m else ""

    found: list[str] = []

    def walk(node):
        if len(found) > 3:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                if any(tag in key for tag in ("代码", "code")) and isinstance(v, str):
                    m = _CODE_RE.search(v)
                    if m:
                        found.append(normalize_code(m.group(0)))
                walk(v)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item)
        elif isinstance(node, str):
            m = _CODE_RE.search(node)
            if m:
                found.append(normalize_code(m.group(0)))

    walk(data)
    return next((c for c in found if c), "")


def _base_term(term: str) -> str:
    t = (term or "").strip()
    for w in _SUFFIX_WORDS:
        if t.endswith(w) and len(t) > len(w):
            t = t[: -len(w)]
            break
    return t.strip()


def _default_db_path() -> str:
    try:
        from utils.app_paths import get_market_data_db_path
        return get_market_data_db_path()
    except Exception:
        return ""


def lookup_db(name: str, db_path: Optional[str] = None) -> str:
    """在本地库里按名称找代码（指数 → 板块 → 个股）；找不到返回空串。"""
    cands = search(name, db_path)
    return cands[0]["code"] if cands else ""


def search(name: str, db_path: Optional[str] = None, limit: int = 5) -> list[dict]:
    """名称 → **候选列表**（按匹配度排序）。

    为什么不直接返回唯一解：像「银行板块」这种词，库里同时有「上证银行」
    「中证银行指数」「300银行」——谁对取决于上下文，代码硬猜就是新的硬编码。
    这里只做**无歧义的排序**（精确 > 前缀 > 后缀 > 包含），把选择权交回 LLM。
    """
    term = _base_term(name)
    if not term:
        return []
    path = db_path or _default_db_path()
    if not path or not Path(path).exists():
        return []
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    try:
        cands: list[dict] = []
        for table, code_col, name_col, kind in (
                ("index_info", "code", "name", "index"),
                ("stock_board", "board_code", "board_name", "board"),
                ("stock_info", "code", "name", "stock")):
            for row in _matches(conn, table, code_col, name_col, term):
                code = str(row["code"])
                if kind == "index":
                    code = normalize_index_code(code)
                elif kind == "stock":
                    code = normalize_code(code)
                cands.append({"code": code, "name": str(row["name"]), "kind": kind,
                              "score": _score(str(row["name"]), term, kind)})
        # 先按标的类型（指数 > 板块 > 个股：技能工具/多源 fetcher 对指数支持最好），
        # 再按字面匹配度。类型优先是**能力可达性**约束，不是名称硬编码。
        kind_rank = {"index": 0, "board": 1, "stock": 2}
        cands.sort(key=lambda c: (kind_rank.get(c["kind"], 9), -c["score"]))
        seen, out = set(), []
        for c in cands:
            if c["code"] in seen:
                continue
            seen.add(c["code"])
            out.append(c)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def _score(name: str, term: str, kind: str) -> float:
    """匹配度：精确 > 加「指数」后缀 > 前缀 > 后缀 > 包含；同类里名称越短越靠前。"""
    base = {"index": 3.0, "board": 2.0, "stock": 1.0}.get(kind, 0.0)
    if name == term:
        return 100 + base
    if name == term + "指数":
        return 95 + base
    if name.startswith(term):
        return 85 + base - len(name) * 0.05
    if name.endswith(term):
        return 80 + base - len(name) * 0.05
    return 60 + base - len(name) * 0.1


def _matches(conn, table: str, code_col: str, name_col: str, term: str) -> list:
    try:
        return conn.execute(
            f"SELECT {code_col} AS code, {name_col} AS name FROM {table} "
            f"WHERE {name_col} LIKE ? ORDER BY LENGTH({name_col}) LIMIT 20",
            (f"%{term}%",)).fetchall()
    except sqlite3.Error:
        return []


def resolve(universe: str, *, query_fn: Optional[Callable[[str], str]] = None,
            db_path: Optional[str] = None) -> str:
    """人话 → 代码；解析不出返回空串（调用方降级，不猜）。"""
    text = (universe or "").strip()
    if not text:
        return ""
    if looks_like_code(text):
        return normalize_code(text)
    m = _CODE_RE.search(text)
    if m:
        return normalize_code(m.group(0))

    code = lookup_db(text, db_path)
    # 板块代码（BK1283 / gn_xxx）对技能工具支持不好：能拿到指数代码就用指数
    if code and not re.fullmatch(r"\d{6}(?:\.(?:SZ|SH|BJ))?", code) and query_fn is not None:
        try:
            alt = extract_code(query_fn(f"{text} 指数代码") or "")
            if alt:
                return alt
        except Exception:  # noqa: BLE001
            pass
    if code:
        return code

    if query_fn is not None:
        try:
            payload = query_fn(f"{text} 指数代码 股票代码 板块代码")
            return extract_code(payload or "")
        except Exception:  # noqa: BLE001 —— 解析失败是常态，降级即可
            return ""
    return ""
