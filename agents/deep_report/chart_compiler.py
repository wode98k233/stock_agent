"""图表 spec → ECharts option 编译器。

原则（设计 §7.4 / 评审 P1-6）：
- 图必须带 `source` + `asof`，缺一个就拒绝编译（无源图不得出现）；
- LLM 只产 spec（数据 + 类型），这里负责映射，不让模型写 ECharts 配置；
- M1 覆盖 8 类：line / bar / stacked / combo / heatmap / pie / histogram / scatter / area。
"""
from __future__ import annotations

from typing import Any, Dict, List

SUPPORTED = ("line", "bar", "stacked", "combo", "heatmap", "pie", "histogram", "scatter", "area")

_PALETTE = ["#176b5b", "#2aa08a", "#7f9f97", "#b4534b", "#c9a227", "#5b7db1", "#8a6db1"]


class ChartSpecError(ValueError):
    """spec 不合规（缺 source/asof、类型不支持、series 为空）。"""


def _require(spec: Dict[str, Any], key: str) -> Any:
    val = spec.get(key)
    if val in (None, "", []):
        raise ChartSpecError(f"图 {spec.get('id') or '?'} 缺少必填字段 {key}（无源图不得出现）")
    return val


def compile_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """返回可直接交给 ECharts 的 option 字典。"""
    ctype = str(spec.get("type") or "").lower()
    if ctype not in SUPPORTED:
        raise ChartSpecError(f"不支持的图表类型: {ctype}（支持 {SUPPORTED}）")
    _require(spec, "source")
    _require(spec, "asof")
    series = spec.get("series") or []
    if not series:
        raise ChartSpecError(f"图 {spec.get('id')}: series 为空")

    title = spec.get("title", "")
    unit = spec.get("unit") or {}
    x = (spec.get("x") or {}).get("values") or [s.get("name") for s in series]

    # 形状校验：x 长度必须与每条 series 一致（否则画出来是一坨竖点）
    if ctype in ("line", "bar", "stacked", "combo", "area", "histogram"):
        if not x:
            raise ChartSpecError(f"图 {spec.get('id')}: 缺少 x 轴数据")
        for s in series:
            if len(s.get("data") or []) != len(x):
                raise ChartSpecError(
                    f"图 {spec.get('id')}: x 长度 {len(x)} 与 series「{s.get('name')}」"
                    f"长度 {len(s.get('data') or [])} 不一致")
    if ctype == "pie":
        for s in series:
            data = s.get("data") or []
            if len(data) != 1 or not isinstance(data[0], (int, float)):
                raise ChartSpecError(
                    f"图 {spec.get('id')}: pie 的 series「{s.get('name')}」需要恰好一个数值")

    base: Dict[str, Any] = {
        "title": {"text": title, "left": "center", "top": 2, "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis" if ctype not in ("pie", "heatmap", "scatter") else "item"},
        "color": _PALETTE,
        "grid": {"left": 56, "right": 24, "top": 56, "bottom": 40},
        "textStyle": {"fontSize": 11},
    }

    if ctype == "heatmap":
        data = spec.get("data") or []
        base.update({
            "xAxis": {"type": "category", "data": spec.get("x_labels") or []},
            "yAxis": {"type": "category", "data": spec.get("y_labels") or []},
            "visualMap": {"min": spec.get("min", -1), "max": spec.get("max", 1),
                          "orient": "vertical", "right": 4, "top": 40},
            "series": [{"type": "heatmap", "data": data,
                        "label": {"show": True, "fontSize": 10}}],
        })
        return base

    if ctype == "pie":
        base.update({
            "series": [{
                "type": "pie", "radius": "58%",
                "data": [{"name": s.get("name"), "value": (s.get("data") or [0])[0]} for s in series],
            }],
        })
        return base

    if ctype == "histogram":
        base.update({
            "xAxis": {"type": "category", "data": x},
            "yAxis": {"type": "value", "name": unit.get("y1", "")},
            "series": [{"type": "bar", "data": s.get("data") or [], "barGap": "10%",
                        "name": s.get("name", "")} for s in series],
        })
        return base

    if ctype == "scatter":
        base.update({
            "xAxis": {"type": "value", "name": spec.get("x_name", "")},
            "yAxis": {"type": "value", "name": spec.get("y_name", "")},
            "series": [{"type": "scatter", "symbolSize": 10,
                        "data": s.get("data") or [], "name": s.get("name", "")} for s in series],
        })
        return base

    # 直角坐标系：line / area / bar / stacked / combo
    y_axis: List[Dict[str, Any]] = [{"type": "value", "name": unit.get("y1", "")}]
    if ctype == "combo" or any(s.get("axis") == 2 for s in series):
        y_axis.append({"type": "value", "name": unit.get("y2", "")})

    built: List[Dict[str, Any]] = []
    for s in series:
        kind = "bar" if ctype in ("bar", "combo") and s.get("kind", "bar") == "bar" else "line"
        if ctype == "line":
            kind = "line"
        if ctype == "area":
            kind = "line"
        item: Dict[str, Any] = {"name": s.get("name", ""), "type": kind,
                                "data": s.get("data") or [],
                                "yAxisIndex": 1 if s.get("axis") == 2 else 0}
        if ctype == "stacked":
            item["stack"] = "total"
        if ctype in ("line", "area", "combo") and kind == "line":
            item["smooth"] = True
        if ctype == "area":
            item["areaStyle"] = {"opacity": 0.18}
        built.append(item)

    base.update({
        "legend": {"top": 6, "itemWidth": 10, "itemHeight": 8},
        "xAxis": {"type": "category", "data": x, "boundaryGap": ctype not in ("line", "area")},
        "yAxis": y_axis,
        "series": built,
    })
    return base


def compile_all(specs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """编译全部；单个失败不影响其它（失败项记在返回的 `_errors`）。"""
    out: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    for spec in specs or []:
        cid = str(spec.get("id") or f"c{len(out) + 1}")
        try:
            out[cid] = compile_spec(spec)
        except ChartSpecError as e:
            errors[cid] = str(e)
    if errors:
        out["_errors"] = errors  # type: ignore[assignment]
    return out
