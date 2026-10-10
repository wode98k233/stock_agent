"""台账定义加载与校验 —— `data/thesis/*.json`。

为什么单独一个模块：定义既被技能（`tools/skills/dividend_env`）读，也被台账服务/页面读，
且需要「拒绝坏定义」的显式校验——阈值表少覆盖一段区间就会静默算不出分，
这类问题必须在加载期拦住，而不是等在页面上看到「—」再回头查。
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 区间打分表的得分取值范围（与阈值语义绑定：-2 不利 ~ +2 很有利）
SCORE_MIN, SCORE_MAX = -2, 2

# 打分覆盖性检查用的「伪无穷」：首行下限需 <= 此值、末行上限需 >= 此值。
# 与 SCORE_MIN/SCORE_MAX 是两件事：那两个是**得分**范围（-2~2），
# 这两个是**读数**的兜底边界，惯例值 -99 / 99（bond_fund / dividend_env /
# gold_regime / hang_seng_tech 四张台账一致）。改这两个值等于改动「合法定义」的
# 判定门槛——上调会让所有既有台账凭空报「未覆盖」假警，务必同步全部 JSON。
_NEG_INF, _POS_INF = -99, 99

# 支持在信号 / 失效条件里引用的指标键
KNOWN_METRICS = {
    "premium", "dy", "cn10_y", "crowding", "style_rel",
    "insurance_gap", "insurance_stale", "dy_rank",
}
KNOWN_OPS = {">", "<", ">=", "<=", "is_true"}

_cache: dict = {}


def get_defs_dir() -> Path:
    """台账定义目录（走 app_paths，兼容打包后的资源根）。"""
    from utils.app_paths import get_data_path
    return Path(get_data_path()) / "thesis"


def list_defs() -> list[str]:
    """列出全部台账 id（= 目录内 *.json 的文件名主干）。"""
    d = get_defs_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def load_def(thesis_id: str) -> dict | None:
    """按 id 加载定义（带 mtime 缓存，改完 JSON 无需重启）。"""
    path = get_defs_dir() / f"{thesis_id}.json"
    if not path.exists():
        return None
    mtime = os.path.getmtime(path)
    key = f"{thesis_id}:{mtime}"
    if key in _cache:
        return _cache[key]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # 坏 JSON 不应让调用方 500
        logger.error("[Thesis] 定义 %s 解析失败: %s", thesis_id, e)
        return None
    _cache.clear()  # 只保留最新版本，避免长进程缓存膨胀
    _cache[key] = data
    return data


def load_def_checked(thesis_id: str) -> tuple[dict | None, list[str]]:
    """加载 + 校验，返回 (定义, 问题列表)。问题非空时定义照常返回供降级渲染。"""
    defn = load_def(thesis_id)
    if defn is None:
        return None, [f"台账定义不存在或无法解析: {thesis_id}"]
    return defn, validate_def(defn)


def validate_def(defn: dict) -> list[str]:
    """校验台账定义。返回问题列表（空 = 通过）。

    检查项按「静默失效风险」排序：阈值覆盖不全 > 指标名写错 > 元信息缺失。
    """
    problems: list[str] = []
    tid = defn.get("id", "?")

    for field in ("id", "name", "version", "subject", "thresholds", "verdicts"):
        if not defn.get(field):
            problems.append(f"[{tid}] 缺字段: {field}")

    subject = defn.get("subject") or {}
    for field in ("index_code", "benchmark_code"):
        if not subject.get(field):
            problems.append(f"[{tid}] subject 缺 {field}（取数口径无法确定）")

    # 阈值表：格式 + 覆盖性
    for name, table in (defn.get("thresholds") or {}).items():
        problems.extend(_validate_threshold_table(tid, name, table))

    # 分数表：首尾必须兜住，否则会出现「取不到分」
    # 注意用 min/max 而非首末行——阈值表可以按降序书写（更贴近「先看高档位」的直觉）
    for name, table in (defn.get("thresholds") or {}).items():
        if not isinstance(table, list) or not table:
            continue
        rows = [r for r in table if isinstance(r, list) and len(r) == 3]
        if not rows:
            continue
        lo_min = min(r[0] for r in rows)
        hi_max = max(r[1] for r in rows)
        if lo_min > _NEG_INF:
            problems.append(
                f"[{tid}] 阈值 {name} 未覆盖低端（最小下限 {lo_min} > {_NEG_INF}），"
                f"低于该值将算不出分（会误报成「数据缺失」）")
        if hi_max < _POS_INF:
            problems.append(
                f"[{tid}] 阈值 {name} 未覆盖高端（最大上限 {hi_max} < {_POS_INF}），"
                f"高于该值将算不出分（会误报成「数据缺失」）")

    # 结论分档：必须能兜住任意总分
    verdicts = defn.get("verdicts") or []
    if verdicts:
        mins = [v.get("min_total") for v in verdicts if isinstance(v, dict)]
        if None in mins:
            problems.append(f"[{tid}] verdicts 存在缺 min_total 的条目")
        elif min(mins) > _NEG_INF:
            problems.append(f"[{tid}] verdicts 未兜底低分（最小 min_total={min(mins)}）")

    # 信号 / 失效条件引用的指标必须已知，否则永远不会命中。
    # 指标全集从定义的 factors 推导（因子计算器会往 metrics 里写什么，定义自己最清楚），
    # 不再硬编码——硬编码会让每个新台账的指标都被误判为「未知指标」。
    known = _known_metrics(defn)
    for group in ("buy", "warn"):
        for item in ((defn.get("signals") or {}).get(group) or []):
            problems.extend(_validate_rule(tid, f"signals.{group}", item, known))
    for rule in ((defn.get("invalidation") or {}).get("rules") or []):
        problems.extend(_validate_rule(tid, "invalidation", rule, known))

    # 手工项必须有陈旧阈值，否则「手工数据永久有效」这个隐患会静默存在
    for key, item in (defn.get("manual") or {}).items():
        if key.startswith("_"):
            continue
        if not isinstance(item, dict):
            continue
        if not item.get("stale_days"):
            problems.append(f"[{tid}] manual.{key} 缺 stale_days（手工数据无法判陈旧）")
        if not item.get("updated_at"):
            problems.append(f"[{tid}] manual.{key} 缺 updated_at")

    for p in problems:
        logger.warning("[Thesis] %s", p)
    return problems


def _validate_threshold_table(tid: str, name: str, table) -> list[str]:
    problems: list[str] = []
    if not isinstance(table, list) or not table:
        return [f"[{tid}] 阈值 {name} 不是非空列表"]
    for i, row in enumerate(table):
        if not isinstance(row, list) or len(row) != 3:
            problems.append(f"[{tid}] 阈值 {name}[{i}] 格式错误，应为 [下限, 上限, 得分]")
            continue
        lo, hi, sc = row
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            problems.append(f"[{tid}] 阈值 {name}[{i}] 上下限非数值")
            continue
        if lo >= hi:
            problems.append(f"[{tid}] 阈值 {name}[{i}] 下限 {lo} >= 上限 {hi}（空区间）")
        if not isinstance(sc, int) or not (SCORE_MIN <= sc <= SCORE_MAX):
            problems.append(f"[{tid}] 阈值 {name}[{i}] 得分 {sc} 超出 [{SCORE_MIN},{SCORE_MAX}]")
    return problems


def _known_metrics(defn: dict) -> set:
    """从定义的 factors 推导「信号/失效条件可引用的指标全集」。"""
    known = set(KNOWN_METRICS)  # 兼容未声明 factors 的旧定义
    for spec in (defn.get("factors") or []):
        if not isinstance(spec, dict):
            continue
        # raw_metric 是「同一个因子的原始读数」（如金银比的比值本身、ETF 库存的吨数），
        # 与被打分的变动率分开：信号里经常需要写「库存 < 1000 吨」这种水平条件。
        for field in ("metric", "stale_metric", "rank_metric", "raw_metric"):
            if spec.get(field):
                known.add(spec[field])
        # 因子显式声明的附加指标键（如手工项的派生指标 sow_dev_pct：
        # 读数确实是 3780 万头，但阈值/信号作用在「较高点的去化幅度」上——
        # 两个键都由计算器写进 metrics，故都要可引用）。
        for extra in (spec.get("metrics") or []):
            if extra:
                known.add(extra)
        # rate_premium 会把两个分量的原始读数也写进 metrics（键 = 数据别名）
        if spec.get("compute") == "rate_premium":
            known.update(x for x in (spec.get("dy"), spec.get("rate")) if x)
    return known


def _validate_rule(tid: str, where: str, item, known_metrics: set) -> list[str]:
    if not isinstance(item, dict):
        return [f"[{tid}] {where} 条目不是对象"]
    problems = []
    metric = item.get("metric")
    if metric not in known_metrics:
        problems.append(f"[{tid}] {where} 引用了未知指标 '{metric}'（永远不会命中）")
    op = item.get("op")
    if op not in KNOWN_OPS:
        problems.append(f"[{tid}] {where} 非法运算符 '{op}'")
    if op != "is_true" and not isinstance(item.get("value"), (int, float)):
        problems.append(f"[{tid}] {where} 缺数值阈值 value")
    if where.startswith("signals") and not item.get("label"):
        problems.append(f"[{tid}] {where} 缺 label")
    if where == "invalidation" and not item.get("text"):
        problems.append(f"[{tid}] {where} 缺 text（失效条件需给出人话解释）")
    return problems


def score_by_table(value, table) -> int | None:
    """区间打分：每行 [下限, 上限, 得分]，下限含、上限不含。取不到返回 None。"""
    if value is None:
        return None
    for row in table or []:
        if not isinstance(row, list) or len(row) != 3:
            continue
        lo, hi, sc = row
        if lo <= value < hi:
            return sc
    return None


def resolve(thresholds: dict, key: str):
    """按名字取阈值表（缺失返回空表，由调用方标数据缺失而非静默给 0 分）。"""
    return (thresholds or {}).get(key) or []
