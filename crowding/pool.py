"""拥挤度标的池与阈值定义（`data/crowding/pool.json`，可手改、进版本管理）。

设计要点：
- **阈值表沿用台账的 `[下限, 上限, 得分]`** 格式（下限含、上限不含）。分项判分是
  绝对刻度而不是池内分位：池子大小会变（板块池会随采集推进而扩张），分位阈值不可复现；
  绝对刻度可解释、可回归、不会因为池子变大而让所有标的"变拥挤"。
- **必须兜住 -99 ~ 99 两端**（老坑：上限写 99 而下一个区间从 99 起，会让值落空静默变
  「数据缺失」）。`validate_pool` 显式检查每一档的覆盖与空区间。
- **权重失配要能发现**：`weights` 之和与 1 相差超过 0.01 会被校验报出（引擎仍会归一化，
  不会算出 >100 的分，但定义写错必须让人看见）。
"""
from __future__ import annotations

import copy
import json
import os

# 默认池：宽基 + 主要行业主题指数（取自 index_info 的既有覆盖），板块池默认全量但分批补数。
DEFAULT_POOL: dict = {
    "version": "1.0",
    "name": "全市场拥挤度标的池",
    "description": "指数池 + 东财行业板块池；拥挤度 = 长期均线延伸度 / 20 日动量 / 放量比 / 60 日追高 的加权合成",
    "indices": {
        "mode": "db",            # db = 取 index_info 里已有的指数；explicit = 只用 include
        "include": [],
        "exclude": ["399300"],   # 「沪深300深」口径重复，排除
        "max": 60,
    },
    "boards": {
        "enabled": True,
        "provider": "akshare",
        "board_type": "industry",
        "mode": "all",           # all / explicit
        "include": [],
        "exclude": [],           # 板块名（不含 board_ 前缀），如 ["其他数字媒体"]
        "max": 600,
        # 单轮最多补多少个标的的日线。上游每次调用有 2–5s 随机限流，
        # 496 个板块一轮全补要几十分钟，所以用游标分批推进；算分不受此限（纯本地读）。
        "collect_limit_per_run": 150,
    },
    "params": {
        "ma_days": 250,          # 长期均线窗口（延伸度）
        "min_rows": 120,         # 参与计算的最低日线行数
        "hist_days": 400,        # 采集目标行数
        "stale_days": 7,         # 日线最新日期超过该天数 → 标陈旧、不计入拥挤榜
        "top_n": 60,             # 明细返回条数上限
        "hot_score": 75.0,       # 判定为「拥挤/过热」的分数下限（用于 hot_count）
    },
    "weights": {"ext": 0.30, "mom": 0.25, "vol": 0.25, "chase": 0.20},
    "thresholds": {
        # 长期均线延伸度（%）—— 价格离 250 日均线多远
        "ext": [[-99, -5, 10], [-5, 0, 25], [0, 8, 40], [8, 18, 60], [18, 30, 78], [30, 99, 95]],
        # 20 日动量（%）—— 短期资金推力
        "mom": [[-99, 0, 15], [0, 4, 35], [4, 10, 55], [10, 18, 75], [18, 99, 95]],
        # 放量比（近 5 日均额 / 近 60 日均额）—— 成交拥挤
        "vol": [[-99, 0.8, 15], [0.8, 1.1, 35], [1.1, 1.5, 58], [1.5, 2.2, 78], [2.2, 99, 95]],
        # 60 日涨幅（%）—— 追高程度（长周期涨太多 = 抱团松动风险）
        "chase": [[-99, 0, 15], [0, 12, 35], [12, 28, 58], [28, 50, 78], [50, 999, 95]],
    },
    "levels": [[40, "舒适"], [60, "中性"], [75, "偏挤"], [85, "拥挤"], [101, "过热"]],
    "labels": {"ext": "均线延伸", "mom": "20日动量", "vol": "放量比", "chase": "60日追高"},
}

_THRESHOLD_KEYS = ("ext", "mom", "vol", "chase")


def pool_path() -> str:
    from utils.app_paths import get_crowding_pool_path
    return get_crowding_pool_path()


def _deep_merge(base: dict, override: dict) -> dict:
    """默认值兜底：用户只写关心的字段也能跑（列表整体覆盖，不做元素级合并）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_pool(path: str | None = None) -> tuple[dict, list[str]]:
    """加载标的池定义。返回 (pool, problems)；文件缺失/损坏时回退内置默认池并记问题。"""
    problems: list[str] = []
    target = path if path is not None else pool_path()
    raw = None
    if target and os.path.isfile(target):
        try:
            with open(target, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                problems.append(f"池配置根节点不是对象：{target}")
                raw = None
        except Exception as e:  # noqa: BLE001
            problems.append(f"池配置解析失败（已回退内置默认）：{e}")
            raw = None
    else:
        problems.append(f"池配置不存在（已用内置默认，可写一份到 {target}）")

    pool = _deep_merge(DEFAULT_POOL, raw or {})
    problems.extend(validate_pool(pool))
    return pool, problems


def _validate_thresholds(name: str, table: list) -> list[str]:
    """阈值表校验：本档覆盖 + 不重叠 + 两端兜底。"""
    problems: list[str] = []
    if not isinstance(table, list) or not table:
        return [f"阈值表 {name} 为空"]
    sorted_tbl = sorted(table, key=lambda r: r[0] if isinstance(r, (list, tuple)) and r else 0)
    for row in sorted_tbl:
        if not (isinstance(row, (list, tuple)) and len(row) == 3):
            problems.append(f"阈值表 {name} 存在非 [下限,上限,得分] 行：{row!r}")
            return problems
        lo, hi, _score = row
        if not lo < hi:
            problems.append(f"阈值表 {name} 区间为空 [{lo}, {hi})——值落进来会静默变成「数据缺失」")
    first_lo = sorted_tbl[0][0]
    last_hi = sorted_tbl[-1][1]
    if first_lo > -99:
        problems.append(f"阈值表 {name} 未兜住下界（首档下限 {first_lo} > -99）")
    if last_hi < 99:
        problems.append(f"阈值表 {name} 未兜住上界（末档上限 {last_hi} < 99）")
    for a, b in zip(sorted_tbl, sorted_tbl[1:]):
        if b[0] > a[1]:
            problems.append(f"阈值表 {name} 存在缺口：[{a[0]}, {a[1]}) 与 [{b[0]}, {b[1]})")
    return problems


def validate_pool(pool: dict) -> list[str]:
    """静态校验定义。返回问题列表（空 = 干净）。"""
    problems: list[str] = []
    thresholds = pool.get("thresholds") or {}
    for key in _THRESHOLD_KEYS:
        if key not in thresholds:
            problems.append(f"缺少阈值表 thresholds.{key}")
            continue
        problems.extend(_validate_thresholds(key, thresholds[key]))

    weights = pool.get("weights") or {}
    missing = [k for k in _THRESHOLD_KEYS if k not in weights]
    if missing:
        problems.append(f"缺少权重 weights.{','.join(missing)}")
    else:
        total = sum(float(weights[k]) for k in _THRESHOLD_KEYS)
        if abs(total - 1.0) > 0.01:
            problems.append(f"权重之和为 {total:.3f}，偏离 1（引擎会归一化，但定义应修）")

    levels = pool.get("levels")
    if not isinstance(levels, list) or not levels:
        problems.append("缺少 levels 分档表")
    else:
        bounds = [row[0] for row in levels if isinstance(row, (list, tuple)) and row]
        if bounds != sorted(bounds):
            problems.append("levels 分档边界未升序")
        if not bounds or bounds[-1] < 100:
            problems.append("levels 末档未覆盖到 100 分")

    params = pool.get("params") or {}
    for key in ("ma_days", "min_rows", "hist_days", "stale_days"):
        val = params.get(key)
        if not isinstance(val, int) or val <= 0:
            problems.append(f"params.{key} 必须是正整数，当前 {val!r}")
    return problems


def level_of(score: float | None, levels: list) -> str | None:
    """按分档表取档位文案。score 为 None（数据缺失）时返回 None。"""
    if score is None:
        return None
    for bound, label in levels:
        if score < bound:
            return label
    return levels[-1][1] if levels else None
