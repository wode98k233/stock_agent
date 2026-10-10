"""拥挤度 Web 服务：读取最近一轮扫描 / 历史 / 标的趋势 / 池定义。

与 `server/thesis_service.py` 同一套约定：这一层负责「给前端的形状 + 降级话术」，
领域计算留在 `crowding/` 包（CLI 与技能可直接用，不必绕 Web）。
**所有读函数对空库保持宽容**：没跑过扫描时返回 `available=False` 的形状而不是 500——
监控页最怕白屏。
"""
import logging

logger = logging.getLogger("radar.crowding")


def _storage():
    from crowding.storage import CrowdingStorage
    return CrowdingStorage.instance()


def _level_histogram(items: list, levels: list) -> dict:
    """按分档统计数量，保持 levels 的顺序（前端直接按这个顺序画色带）。"""
    order = [label for _bound, label in (levels or [])]
    hist = {label: 0 for label in order}
    for it in items:
        lv = it.get("level")
        if lv in hist:
            hist[lv] += 1
    return hist


def get_latest(limit: int = 400) -> dict:
    """最近一轮扫描：汇总 + 明细。空库返回 available=False 的完整形状。"""
    from crowding.pool import load_pool

    try:
        scan = _storage().latest_scan(with_items=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Crowding] 读最近扫描失败: %s", e)
        return {"available": False, "error": str(e), "items": [],
                "levels": [], "labels": {}, "counts_by_level": {}}

    pool, problems = load_pool()
    if not scan:
        return {
            "available": False, "trade_date": None,
            "levels": pool.get("levels") or [], "labels": pool.get("labels") or {},
            "levels_order": [lbl for _b, lbl in (pool.get("levels") or [])],
            "counts_by_level": {}, "items": [], "pool_problems": problems,
            "pool": {"indices_max": (pool.get("indices") or {}).get("max"),
                     "boards": pool.get("boards") or {}},
        }

    params = scan.get("params") or {}
    items = scan.get("items") or []
    hot = [it for it in items if it.get("status") == "ok"
           and (it.get("score") or 0) >= float(params.get("hot_score") or 75)]
    return {
        "available": True,
        "trade_date": scan.get("trade_date"),
        "scanned_at": scan.get("scanned_at"),
        "elapsed_ms": scan.get("elapsed_ms"),
        "pool_size": scan.get("pool_size"),
        "computed_count": scan.get("computed_count"),
        "insufficient_count": scan.get("insufficient_count"),
        "stale_count": scan.get("stale_count"),
        "hot_count": scan.get("hot_count"),
        "collected_count": scan.get("collected_count"),
        "pool": (params.get("pool") or {}),
        "params": (params.get("params") or {}),
        "weights": (params.get("weights") or {}),
        "labels": (params.get("labels") or {}),
        "levels": (params.get("levels") or pool.get("levels") or []),
        "levels_order": [lbl for _b, lbl in (params.get("levels") or pool.get("levels") or [])],
        "hot_score": params.get("hot_score"),
        "warnings": (params.get("warnings") or [])[:20],
        "pool_problems": problems,
        "counts_by_level": _level_histogram(items, params.get("levels") or pool.get("levels")),
        "hot": hot[:60],
        "items": items[:max(1, limit)],
        "item_total": len(items),
    }


def get_history(days: int = 90) -> dict:
    try:
        items = _storage().list_scans(days=days)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Crowding] 读历史失败: %s", e)
        return {"items": [], "count": 0, "error": str(e)}
    return {"items": items, "count": len(items)}


def get_item_history(item_key: str, limit: int = 180) -> dict:
    try:
        items = _storage().item_history(item_key, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Crowding] 读标的趋势(%s)失败: %s", item_key, e)
        return {"item_key": item_key, "items": [], "count": 0, "error": str(e)}
    return {"item_key": item_key, "items": items, "count": len(items)}


def get_pool_info() -> dict:
    """当前标的池定义 + 校验问题（页面上直接给「池子怎么配的」一个交代）。"""
    from crowding.pool import load_pool, pool_path
    pool, problems = load_pool()
    return {"path": pool_path(), "pool": pool, "problems": problems}


def scan_now(collect: bool = True, save: bool = True) -> dict:
    """同步跑一轮（CLI / 技能 / 手动排查用）。Web 侧请走后台巡检触发。"""
    from crowding.engine import CrowdingEngine
    report = CrowdingEngine().scan(collect=collect)
    if save:
        report = dict(report)
        report["saved"] = _storage().save_scan(report)
    return report
