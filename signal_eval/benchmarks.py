"""行业基准映射（P6 两步走）：个股 → 东财行业板块 → 行业指数/板块K线 → 沪深300 兜底。

纯本地逻辑：读 market_data.db 的 stock_board_member × stock_board（东财板块
归属快照），优先按关键词表把板块名映射到 index_daily 已有的行业/主题指数
（第一步口径）；第二步起，无关键词指数的板块回落到板块K线
（stock_daily 的 board_industry_X 代码，由采集管道供数）。
映射构建失败或个股无归属时一律兜底沪深300——alpha 计算永不因映射挂掉。

数据说明（2026-09-06 实测）：stock_board 列表 496 个行业板块齐全；
stock_board_member 已随第二步管道全量同步；板块K线按需增量补齐
（utils/cache/market_data_db.py::fetch_and_store_board_kline）。

性能（2026-11 P0）：构建一次要扫 stock_board_member 全表（冷启动实测可到 ~20s）。
因此构建结果落同库目录的 `bench_mapping.json`（`{"ts": epoch, "mapping": {...}}`），
7 天内直接复用，不再查库；板块K线代码改用 PK 前缀范围查询避免全表扫。
写盘失败/目录只读一律忽略，不影响 fail-safe 兜底口径。
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BENCH_FALLBACK = "000300"  # 沪深300 兜底

_MAPPING_DB_PATH: str | None = None  # 测试可 patch；None = 默认 get_market_data_db_path()

# 磁盘快照：与 market_data.db 同目录，7 天内有效（板块归属是低频数据）
_MAPPING_SNAPSHOT = "bench_mapping.json"
_MAPPING_TTL = 7 * 24 * 3600

# 板块K线代码前缀。范围查询上界 = 前缀最后一字节 +1（'_' 0x5F → '`' 0x60）：
# SQLite 的 BINARY 比较按 UTF-8 字节序，'board_industry`' 大于任何以
# 'board_industry_' 开头的串——**含中文板块名**。不能写成追加 chr(0x7f)：
# 中文首字节 ≥ 0xE0 > 0x7F，那样会把 board_industry_半导体 全部排除掉。
_BOARD_CODE_PREFIX = "board_industry_"
_BOARD_CODE_UPPER = _BOARD_CODE_PREFIX[:-1] + chr(ord(_BOARD_CODE_PREFIX[-1]) + 1)


# 行业指数关键词表（专一在前：白酒 → 生物医药 → 医药 → 新能源 → 新能源车 → 消费）。
# 指数必须在 index_daily 采集清单内（backtest/data_collector.py default_indices）。
INDUSTRY_INDEX_KEYWORDS: list[tuple[str, str, str]] = [
    # (指数代码, 指数名, 板块名关键词)
    ("399997", "中证白酒", ("酒",)),
    ("399441", "生物医药", ("生物医药", "生物科技")),
    ("000991", "全指医药", ("药", "医疗", "健康", "医美")),
    ("399808", "中证新能源", ("光伏", "风能", "风电", "储能", "核电", "氢能", "太阳能")),
    ("399976", "新能源车", ("新能源车", "锂电", "电池", "汽车", "充电桩")),
    ("000932", "中证消费", ("食品", "饮料", "乳制品", "农牧", "农业", "养殖",
                            "家电", "服装", "纺织", "零售", "百货", "商业",
                            "旅游", "酒店", "餐饮", "消费", "商贸", "文娱")),
]

_PREFIX_RE = re.compile(r"^(sh|sz|bj)", re.IGNORECASE)

_cache_lock = threading.Lock()
_mapping_cache: dict[str, dict[str, str]] = {}  # db_path -> {code: index_code}


def _bare_code(code: str) -> str:
    """剥市场前缀（sh/sz/bj，大小写不敏感）→ 裸 6 位代码。"""
    return _PREFIX_RE.sub("", (code or "").strip())


def _match_board(board_name: str) -> str | None:
    """板块名按关键词表顺序匹配（专一优先），返回指数代码或 None。"""
    for index_code, _label, keywords in INDUSTRY_INDEX_KEYWORDS:
        if any(kw in board_name for kw in keywords):
            return index_code
    return None


def _load_mapping_from_db(db_path: str) -> dict[str, str]:
    """从板块归属构建个股映射（只读短连接，失败返回空）。

    优先级（P6 两步走）：
    1. 关键词表 → index_daily 行业指数（第一步口径不变，权威指数优先）
    2. 板块K线 → stock_daily 的 board_industry_X（第二步，成分数最少的
       板块最专一优先；无关键词指数的板块由此获得基准）
    3. 都没有 → 不映射（调用方兜底 000300）
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT m.code, b.board_id, b.board_name FROM stock_board_member m"
                " JOIN stock_board b ON b.board_id = m.board_id"
                " WHERE b.board_type = 'industry' AND b.is_active = 1"
                " ORDER BY m.code").fetchall()
            # 各板块成分数（专一优先：成分越少越聚焦）
            counts = {r[0]: r[1] for r in conn.execute(
                "SELECT board_id, COUNT(*) FROM stock_board_member GROUP BY board_id")}
            # 可用板块K线（board_industry_ 前缀；旧库无 stock_daily 表时视为无）。
            # 走 PK(code, trade_date) 的前缀范围，避免对全表做 LIKE 扫描。
            try:
                board_codes = {r[0] for r in conn.execute(
                    "SELECT DISTINCT code FROM stock_daily WHERE code >= ? AND code < ?",
                    (_BOARD_CODE_PREFIX, _BOARD_CODE_UPPER))}
            except sqlite3.Error:
                board_codes = set()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("[Bench] 行业映射读取失败（全兜底沪深300）: %s", e)
        return {}

    # 按股分组；板块按（成分数升序, board_id）排序 → 最专一优先且确定性
    boards_by_stock: dict[str, list[tuple[int, str, str]]] = {}
    for code, board_id, board_name in rows:
        bare = _bare_code(code)
        if not bare:
            continue
        boards_by_stock.setdefault(bare, []).append(
            (counts.get(board_id, 0), board_id, board_name or ""))

    mapping: dict[str, str] = {}
    for bare, boards in boards_by_stock.items():
        for _cnt, _bid, name in sorted(boards):
            index_code = _match_board(name)
            if index_code:
                mapping[bare] = index_code
                break
            board_code = f"board_industry_{name}"
            if board_code in board_codes:
                mapping[bare] = board_code
                break
    return mapping


def _mapping_file_path(db_path: str) -> Path:
    """磁盘快照路径：与 market_data.db 同目录（显式 db_path 也用它自己的目录）。"""
    return Path(db_path).parent / _MAPPING_SNAPSHOT


def _load_mapping_from_disk(db_path: str) -> dict[str, str] | None:
    """读磁盘快照：< 7 天且内容非空 → 直接用；缺失/损坏/过期 → None（回落到构建）。"""
    try:
        path = _mapping_file_path(db_path)
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        ts = float(payload.get("ts") or 0)
        if not ts or time.time() - ts > _MAPPING_TTL:
            return None
        mapping = payload.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            return None
        return {str(k): str(v) for k, v in mapping.items()}
    except Exception as e:  # 文件不存在/坏 JSON/权限问题都不阻断主流程
        logger.debug("[Bench] 磁盘映射快照不可用: %s", e)
        return None


def _save_mapping_to_disk(db_path: str, mapping: dict[str, str]) -> None:
    """原子写回快照（tmp + replace）；空映射/写失败一律忽略。"""
    if not mapping:
        return
    path = _mapping_file_path(db_path)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "mapping": mapping}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # 只读目录 / 并发写 / 磁盘满：忽略
        logger.debug("[Bench] 磁盘映射快照写入失败（忽略）: %s", e)
        try:
            tmp.unlink()
        except Exception:
            pass


def build_bench_mapping(db_path: str | None = None) -> dict[str, str]:
    """构建 {股票代码: 行业指数代码} 映射（内存 → 磁盘快照 → 读库构建，逐级回落）。

    - 同库只在内存里构建/读取一次；
    - 内存未命中先读同目录 `bench_mapping.json`（<7 天 → 直接用，不查库）；
    - 仍没有 → 读库构建，成功后原子写回快照（供下次进程冷启动秒开）。
    """
    path = db_path or _MAPPING_DB_PATH
    if path is None:
        try:
            from config import Config
            path = Config.get_market_data_db_path()
        except Exception as e:
            logger.debug("[Bench] 取 market_data.db 路径失败: %s", e)
            return {}
    with _cache_lock:
        cached = _mapping_cache.get(path)
        if cached is not None:
            return cached
        mapping = _load_mapping_from_disk(path)
        if mapping is not None:
            _mapping_cache[path] = mapping
            logger.info("[Bench] 行业基准映射命中磁盘快照: %d 只个股", len(mapping))
            return mapping
        _mapping_cache[path] = _load_mapping_from_db(path)
        logger.info("[Bench] 行业基准映射构建完成: %d 只个股命中行业指数",
                    len(_mapping_cache[path]))
        _save_mapping_to_disk(path, _mapping_cache[path])
        return _mapping_cache[path]


def reset_cache() -> None:
    """只清内存映射缓存（测试用）；不动磁盘快照，下次构建会命中文件。

    ⚠️ 这也意味着「清内存 + 同目录还有 <7 天快照」时不会重新查库——想验证
    重建路径需先删掉 `bench_mapping.json`（或改 ts），见 test/unit/signal_eval/
    test_benchmarks.py::TestMappingSnapshot。
    """
    with _cache_lock:
        _mapping_cache.clear()


def resolve_benchmark(code: str) -> str:
    """解析个股的行业基准指数；无映射/异常一律兜底沪深300。"""
    try:
        mapping = build_bench_mapping()
        return mapping.get(_bare_code(code), BENCH_FALLBACK)
    except Exception as e:
        logger.debug("[Bench] 基准解析失败，兜底沪深300: %s", e)
        return BENCH_FALLBACK
