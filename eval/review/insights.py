"""改进清单（设计文档 §10）：聚合所有已完成评审的 findings，按频次排序。

「单条 finding 是一个问题，聚合后才是先改什么」。查询时聚合
（reviews.findings_json 是 JSON，SQLite 无 GROUP BY JSON，在 Python 聚合），
按 `(dimension, attribution)` 分组：出现频次 → 涉及目标 → 示例 verdict。
"""
from __future__ import annotations

import json


def _loads(value) -> list:
    if not isinstance(value, str):
        return value if isinstance(value, list) else []
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def build_improvement_list(conn, *, rubric_versions: set[str] | None = None,
                           limit: int = 20) -> list[dict]:
    """聚合 done 评审的 findings → 改进清单（按出现频次降序）。

    Args:
        rubric_versions: 只聚合这些评审版本的 findings；None 不过滤。
            前端展示建议传当前版本集（旧版本维度键不同，混入会脏）。
        limit: 返回条目上限。

    Returns:
        [{dimension, attribution, count, sample_verdicts, targets}]，
        targets 形如 `run:<run_id>` / `dialog:<dialog_uuid>`。
    """
    rows = conn.execute(
        "SELECT scope, target_id, rubric_version, findings_json FROM reviews"
        " WHERE status='done'").fetchall()

    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        if rubric_versions is not None and \
                (row["rubric_version"] or "") not in rubric_versions:
            continue
        scope = row["scope"] or "run"
        target_key = f"{scope}:{row['target_id']}"
        for finding in _loads(row["findings_json"]):
            if not isinstance(finding, dict):
                continue
            dimension = (finding.get("dimension") or "").strip()
            if not dimension:
                continue
            attribution = (finding.get("attribution") or "").strip() or "未归因"
            verdict = (finding.get("verdict") or "").strip()
            key = (dimension, attribution)
            item = groups.setdefault(key, {
                "dimension": dimension, "attribution": attribution,
                "count": 0, "sample_verdicts": [], "targets": [],
            })
            item["count"] += 1
            if target_key not in item["targets"]:
                item["targets"].append(target_key)
            if verdict and verdict not in item["sample_verdicts"] \
                    and len(item["sample_verdicts"]) < 3:
                item["sample_verdicts"].append(verdict)

    items = sorted(groups.values(), key=lambda x: (-x["count"], x["dimension"]))
    return items[:limit]
