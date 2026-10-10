"""ETF 标的识别（P4）：别名词典 + 本地 stock_info 名称匹配。

背景：`extract_symbol` 的中文名滑窗只查 `s_` 前缀个股，ETF 在 stock_info
是裸代码 + market='ETF' 且基金全名冗长——自然语言「分析下红利ETF」无法命中。

两层解析：
1. 硬编码高频简称表（红利ETF/消费ETF/黄金ETF…）
2. 本地 stock_info ETF 行按关键词匹配（「红利低波动ETF」→ 含「红利低波动」的基金）

命中统一返回 symbol_key（'sh510880' / 'sz159928'），未命中返回 None（不误标）。
"""
import os
import re
import sqlite3

from utils.app_paths import get_market_data_db_path

STOCK_INFO_DB_PATH = get_market_data_db_path()

# 硬编码高频简称 → 裸代码（market ETF，5 开头→sh / 1 开头→sz）
# 表按「更具体的简称在前」排序（如 红利低波ETF 优先于 红利ETF）
_ETf_ALIASES: list[tuple[str, str]] = [
    ("红利低波ETF", "512890"),
    ("红利低波", "512890"),
    ("红利ETF", "510880"),
    ("上证红利", "510880"),
    ("消费ETF", "159928"),
    ("中证消费", "159928"),
    ("黄金ETF", "518880"),
    ("创业板ETF", "159915"),
    ("科创板ETF", "588080"),
    ("科创50", "588080"),
    ("沪深300ETF", "510300"),
    ("沪深300", "510300"),
    ("中证500ETF", "510500"),
    ("中证500", "510500"),
    ("上证50ETF", "510050"),
    ("上证50", "510050"),
    ("券商ETF", "512000"),
    ("银行ETF", "512800"),
    ("医药ETF", "512010"),
    ("医疗ETF", "512170"),
    ("半导体ETF", "512480"),
    ("芯片ETF", "159995"),
    ("新能源ETF", "516160"),
    ("光伏ETF", "515790"),
    ("军工ETF", "512660"),
    ("酒ETF", "512690"),
    ("恒生科技ETF", "513130"),
    ("恒生科技", "513130"),
    ("恒生互联", "513330"),
    ("纳指ETF", "513100"),
    ("标普ETF", "513500"),
    ("游戏传媒ETF", "517770"),
    ("游戏传媒", "517770"),
    ("创新药ETF", "159570"),
    ("港股通创新药ETF", "159570"),
    ("恒生医疗ETF", "513060"),
    ("上海金ETF", "518600"),
    ("红利低波100ETF", "515100"),
]

# 词典命中缓存（进程内）：别名 → symbol_key | None
_alias_cache: dict[str, str | None] = {}

# ETF 基金名行缓存（进程内，按 DB 路径失效重载）：
# 旧实现每次关键词匹配都全表扫一行 + 开连接，重扫数百次滑窗时开销放大。
_etf_rows_cache: list[tuple[str, str]] | None = None
_etf_rows_path: str | None = None

_ALIAS_STRIP_RE = re.compile(r"(交易型开放式指数证券投资基金|交易型开放式指数基金|开放式证券投资基金|证券投资基金|ETF|LOF|联接基金|指数|\(|\)|QDII)")


def etf_symbol_key(code: str) -> str | None:
    """裸 ETF 代码 → symbol_key（5/58 开头→sh，1 开头→sz）；非法返回 None。"""
    if not code or not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith(("5",)):
        return f"sh{code}"
    if code.startswith(("1",)):
        return f"sz{code}"
    return None


def _etf_rows() -> list[tuple[str, str]]:
    """读取（缓存）本地 stock_info 全部 ETF 行：code / 基金全名。DB 路径变化自动重载。"""
    global _etf_rows_cache, _etf_rows_path
    path = STOCK_INFO_DB_PATH
    if _etf_rows_cache is not None and _etf_rows_path == path:
        return _etf_rows_cache
    try:
        conn = sqlite3.connect(path)
        try:
            rows = list(conn.execute("SELECT code, name FROM stock_info WHERE market='ETF'").fetchall())
        finally:
            conn.close()
    except Exception:
        rows = []
    _etf_rows_cache = rows
    _etf_rows_path = path
    return rows


def warmup_etf_rows() -> None:
    """预热 ETF 行缓存（scan_messages 入口调用一次，避免并行解析阶段重复触发全表读）。"""
    _etf_rows()


def _match_by_name(keyword: str) -> str | None:
    """按关键词在本地 stock_info ETF 行匹配（基金全名切词后含关键词即命中）。

    误判防护：关键词过短（<3 字，如「大盘」「医疗」）不参与目录名称匹配——
    大盘日评正文里大量出现「大盘」，子串包含会误命中「创业板大盘ETF招商」；
    硬编码词典（_ETf_ALIASES）不受此限（「恒生互联」4 字、「酒ETF」1 字 stem
    是精确收录的高频别名）。
    """
    if len(keyword) < 3:
        return None
    for code, name in _etf_rows():
        cleaned = _ALIAS_STRIP_RE.sub("", name or "")
        if keyword in cleaned:
            key = etf_symbol_key(code)
            if key:
                return key
    return None


def resolve_etf_alias(alias: str) -> str | None:
    """ETF 简称/别名 → symbol_key；未命中返回 None。

    先查硬编码词典（精确+缓存），再按名称关键词匹配本地 ETF 行。
    输入大小写不敏感。
    """
    if not alias:
        return None
    a = alias.strip()
    key = _alias_cache.get(a)
    if key is not None or a in _alias_cache:
        return key
    lower = a.lower()
    stem = a[:-3] if lower.endswith("etf") else a
    key = None
    for alias_name, code in _ETf_ALIASES:
        alias_stem = alias_name[:-3] if alias_name.lower().endswith("etf") else alias_name
        if alias_name.lower() == lower or alias_stem == stem:
            key = etf_symbol_key(code)
            break
    if key is None and stem:
        key = _match_by_name(stem)
    _alias_cache[a] = key
    return key
