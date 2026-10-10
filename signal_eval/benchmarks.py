"""行业基准映射（P6 两步走）：个股 → 东财行业板块 → 行业指数/板块K线 → 沪深300 兜底。

纯本地逻辑：读 market_data.db 的 stock_board_member × stock_board（东财板块
归属快照），优先按关键词表把板块名映射到 index_daily 已有的行业/主题指数
（第一步口径）；第二步起，无关键词指数的板块回落到板块K线
（stock_daily 的 board_industry_X 代码，由采集管道供数）。
映射构建失败或个股无归属时一律兜底沪深300——alpha 计算永不因映射挂掉。

数据说明（2026-09-06 实测）：stock_board 列表 496 个行业板块齐全；
stock_board_member 已随第二步管道全量同步；板块K线按需增量补齐
（utils/cache/market_data_db.py::fetch_and_store_board_kline）。
"""
import logging
import re
import sqlite3
import threading

logger = logging.getLogger(__name__)

BENCH_FALLBACK = "000300"  # 沪深300 兜底

_MAPPING_DB_PATH: str | None = None  # 测试可 patch；None = 默认 get_market_data_db_path()

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
            # 可用板块K线（board_industry_ 前缀；旧库无 stock_daily 表时视为无）
            try:
                board_codes = {r[0] for r in conn.execute(
                    "SELECT DISTINCT code FROM stock_daily WHERE code LIKE 'board_industry_%'")}
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


def build_bench_mapping(db_path: str | None = None) -> dict[str, str]:
    """构建 {股票代码: 行业指数代码} 映射（模块级缓存，同库只构建一次）。"""
    path = db_path or _MAPPING_DB_PATH
    if path is None:
        try:
            from config import Config
            path = Config.get_market_data_db_path()
        except Exception as e:
            logger.debug("[Bench] 取 market_data.db 路径失败: %s", e)
            return {}
    with _cache_lock:
        if path not in _mapping_cache:
            _mapping_cache[path] = _load_mapping_from_db(path)
            logger.info("[Bench] 行业基准映射构建完成: %d 只个股命中行业指数",
                        len(_mapping_cache[path]))
        return _mapping_cache[path]


def reset_cache() -> None:
    """清空映射缓存（测试收尾 / 板块数据重同步后可调）。"""
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
