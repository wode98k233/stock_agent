"""快照差分 → 变更告警。纯函数、无 IO，便于单独验证。

台账的价值一半在「能重复算」，另一半在「结论变了会主动说」。本模块只负责后者：
对比相邻两次快照，产出四类告警。

告警只报**变化**，不报状态：
- 结论失效条件新命中（critical）
- 综合结论档位变化（warn）
- 因子评分翻转（分数下降=warn，上升=info）
- 因子新变陈旧 / 数据缺失 / 恢复（warn 或 info）

因此「一直陈旧」不会每天刷一条，只有从「正常」变「陈旧」的那一次会报——这正是跟踪台账
需要的信息密度。首次快照（无前值）不产任何告警，硬比就是噪音。

注意：因子 score 越大代表越有利（-2 不利 ~ +2 很有利），故「分数下降」判为恶化。
"""
SEVERITY_ORDER = {"info": 1, "warn": 2, "critical": 3}


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 0)


def _factor_map(snapshot: dict) -> dict:
    return {f.get("key"): f for f in (snapshot.get("factors") or []) if f.get("key")}


def _fmt_value(factor: dict) -> str:
    if not factor:
        return "—"
    v = factor.get("value")
    if v is None:
        return "数据缺失"
    unit = (factor.get("unit") or "").strip()
    try:
        num = float(v)
    except (TypeError, ValueError):
        return str(v)
    text = ("%.0f" if unit == "亿元" else "%.2f") % num
    if unit in ("pp", "%偏离") and num > 0:
        text = "+" + text
    return text + ("" if unit in ("pp", "%偏离") else unit)


def _fmt_score(score) -> str:
    return "—" if score is None else "%+d" % score


def diff_snapshots(prev: dict | None, cur: dict) -> list[dict]:
    """对比相邻快照产出告警；prev 为 None（首次快照）时返回空列表。"""
    if prev is None:
        return []

    thesis_id = cur["thesis_id"]
    trade_date = cur["trade_date"]
    alerts: list[dict] = []

    def add(alert_type: str, severity: str, message: str, detail: dict | None = None):
        alerts.append({
            "thesis_id": thesis_id, "trade_date": trade_date,
            "alert_type": alert_type, "severity": severity,
            "message": message, "detail": detail or {},
        })

    # 1) 结论失效条件新命中 —— 最高优先级，直接说明多头结论不再成立
    prev_inv = {r.get("text"): r for r in (prev.get("invalidation") or [])}
    for rule in (cur.get("invalidation") or []):
        text = rule.get("text")
        if rule.get("hit") and not (prev_inv.get(text) or {}).get("hit"):
            add("invalidation_hit", "critical",
                "结论失效条件命中：%s（当前 %s）" % (text, rule.get("current", "—")),
                {"rule": text, "current": rule.get("current")})

    # 2) 综合结论档位变化
    if prev.get("verdict") != cur.get("verdict"):
        add("verdict_change", "warn",
            "综合结论由「%s」变为「%s」：%s" % (
                prev.get("verdict") or "—", cur.get("verdict") or "—",
                cur.get("action") or "—"),
            {"prev": prev.get("verdict"), "cur": cur.get("verdict"),
             "prev_total": prev.get("total"), "cur_total": cur.get("total")})

    # 3) 因子评分翻转 / 数据缺失 / 恢复
    prev_f, cur_f = _factor_map(prev), _factor_map(cur)
    for key, factor in cur_f.items():
        old = prev_f.get(key)
        if old is None:
            continue
        old_s, new_s = old.get("score"), factor.get("score")
        name = factor.get("name") or key
        if old_s == new_s:
            continue
        if new_s is None:
            add("factor_data_missing", "warn",
                "%s 读数缺失（原为 %s），该因子已退出综合评分" % (name, _fmt_score(old_s)),
                {"factor": key})
        elif old_s is None:
            add("factor_data_restored", "info",
                "%s 数据恢复，读数 %s（评分 %s）" % (name, _fmt_value(factor), _fmt_score(new_s)),
                {"factor": key})
        else:
            worse = new_s < old_s
            add("factor_flip", "warn" if worse else "info",
                "%s 评分 %s → %s（%s），当前 %s" % (
                    name, _fmt_score(old_s), _fmt_score(new_s),
                    factor.get("status") or "—", _fmt_value(factor)),
                {"factor": key, "prev_score": old_s, "cur_score": new_s})

    # 4) 数据新鲜度：只报「新变陈旧」
    for key, factor in cur_f.items():
        old = prev_f.get(key) or {}
        if factor.get("stale") and not old.get("stale"):
            add("factor_stale", "warn",
                "%s 数据源滞后（数据日期 %s），读数偏旧" % (
                    factor.get("name") or key, factor.get("asof") or "—"),
                {"factor": key, "asof": factor.get("asof")})

    # 5) 有效因子数下降 —— 结论的支撑面在变窄
    prev_valid, cur_valid = prev.get("valid_count"), cur.get("valid_count")
    if (prev_valid is not None and cur_valid is not None and cur_valid < prev_valid):
        add("coverage_drop", "warn",
            "有效因子数由 %d 降至 %d，结论支撑面收窄" % (prev_valid, cur_valid),
            {"prev": prev_valid, "cur": cur_valid})

    return alerts


def summarize(alerts: list[dict]) -> dict:
    """按严重度计数（列表页徽标用）。"""
    counts = {"info": 0, "warn": 0, "critical": 0}
    for a in alerts:
        sev = a.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    counts["total"] = len(alerts)
    return counts


def top_severity(alerts: list[dict]) -> str | None:
    if not alerts:
        return None
    return max((a.get("severity", "info") for a in alerts), key=severity_rank)
