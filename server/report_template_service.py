"""报告模板读写服务。"""
import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException


VALID_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def get_report_templates_root() -> Path:
    from utils.app_paths import get_resource_root
    return Path(get_resource_root()) / "agents" / "report_templates"


def _invalidate_runtime_template_caches() -> None:
    from agents.analysis.template_store import invalidate_template_caches

    invalidate_template_caches()


def read_report_templates_index(http_exception_cls=HTTPException) -> dict[str, Any]:
    root = get_report_templates_root()
    index_path = root / "index.json"
    if not index_path.exists():
        raise http_exception_cls(status_code=404, detail="报告模板索引不存在")
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=500, detail=f"报告模板索引 JSON 无效: {exc.msg}")


def safe_template_path(relative_path: str, http_exception_cls=HTTPException) -> Path:
    root = get_report_templates_root().resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise http_exception_cls(status_code=400, detail="模板路径非法")
    if target.name != "template.json":
        raise http_exception_cls(status_code=400, detail="只能编辑 template.json")
    return target


def template_entry(template_id: str, index: dict[str, Any], http_exception_cls=HTTPException) -> dict[str, Any]:
    templates = index.get("templates") or {}
    entry = templates.get(template_id)
    if not isinstance(entry, dict):
        raise http_exception_cls(status_code=404, detail="模板不存在")
    if not entry.get("path"):
        raise http_exception_cls(status_code=400, detail="模板路径缺失")
    return entry


def load_template(template_id: str, index: dict[str, Any] | None = None, http_exception_cls=HTTPException) -> tuple[dict[str, Any], dict[str, Any], Path]:
    index = index or read_report_templates_index(http_exception_cls)
    entry = template_entry(template_id, index, http_exception_cls)
    path = safe_template_path(str(entry["path"]), http_exception_cls)
    if not path.exists():
        raise http_exception_cls(status_code=404, detail="模板文件不存在")
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=500, detail=f"模板 JSON 无效: {exc.msg}")
    return entry, template, path


def load_template_blocks() -> list[dict[str, Any]]:
    root = get_report_templates_root()
    blocks_dir = root / "blocks"
    blocks: list[dict[str, Any]] = []
    if not blocks_dir.exists():
        return blocks
    for path in sorted(blocks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            blocks.append({
                "id": data.get("id") or path.stem,
                "title": data.get("title") or path.stem,
                "required": bool(data.get("required")),
                "max_words": data.get("max_words"),
                "path": f"blocks/{path.name}",
            })
        except Exception:
            blocks.append({
                "id": path.stem,
                "title": path.stem,
                "required": False,
                "path": f"blocks/{path.name}",
                "error": "JSON 读取失败",
            })
    return blocks


def validate_template_payload(template_id: str, template: dict[str, Any], http_exception_cls=HTTPException) -> None:
    if not isinstance(template, dict):
        raise http_exception_cls(status_code=400, detail="template 必须是 JSON 对象")
    payload_id = template.get("id")
    if payload_id and payload_id != template_id:
        raise http_exception_cls(status_code=400, detail="模板 id 不允许改名")
    if "output_blocks" in template and not isinstance(template["output_blocks"], list):
        raise http_exception_cls(status_code=400, detail="output_blocks 必须是数组")
    if "data_contract" in template and not isinstance(template["data_contract"], list):
        raise http_exception_cls(status_code=400, detail="data_contract 必须是数组")
    if "qa_rules" in template and not isinstance(template["qa_rules"], list):
        raise http_exception_cls(status_code=400, detail="qa_rules 必须是数组")
    # 场景元数据类型校验
    if "report_family" in template and template["report_family"] is not None:
        if not isinstance(template["report_family"], str):
            raise http_exception_cls(status_code=400, detail="report_family 必须是字符串")
    if "asset_type" in template and template["asset_type"] is not None:
        if not isinstance(template["asset_type"], str):
            raise http_exception_cls(status_code=400, detail="asset_type 必须是字符串")
    if "max_report_words" in template and template["max_report_words"] is not None:
        if not isinstance(template["max_report_words"], (int, float)):
            raise http_exception_cls(status_code=400, detail="max_report_words 必须是数字")


def save_template_file(template_id: str, template: dict[str, Any], http_exception_cls=HTTPException) -> dict[str, Any]:
    """保存已有模板，并使运行时缓存立即失效。"""
    index = read_report_templates_index(http_exception_cls)
    entry, _, path = load_template(template_id, index, http_exception_cls)
    template = dict(template)
    template.setdefault("id", template_id)
    validate_template_payload(template_id, template, http_exception_cls)
    path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _invalidate_runtime_template_caches()
    return {"id": template_id, "entry": dict(entry), "template": template}


def toggle_template_enabled(template_id: str, enabled: bool, http_exception_cls=HTTPException) -> dict[str, Any]:
    """切换模板启用/禁用状态，写入 index.json。"""
    root = get_report_templates_root()
    index_path = root / "index.json"
    index = read_report_templates_index(http_exception_cls)
    templates = index.get("templates") or {}
    if template_id not in templates:
        raise http_exception_cls(status_code=404, detail="模板不存在")
    templates[template_id]["enabled"] = enabled
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _invalidate_runtime_template_caches()
    return {"id": template_id, "enabled": enabled}


def import_template_file(file_content: bytes, filename: str, http_exception_cls=HTTPException) -> dict[str, Any]:
    """导入单个 template.json 文件。返回 {id, action, conflicts}。"""
    try:
        template = json.loads(file_content)
    except json.JSONDecodeError as exc:
        raise http_exception_cls(status_code=400, detail=f"JSON 解析失败: {exc.msg}")

    if not isinstance(template, dict):
        raise http_exception_cls(status_code=400, detail="template 必须是 JSON 对象")

    template_id = template.get("id")
    if not template_id or not isinstance(template_id, str):
        raise http_exception_cls(status_code=400, detail="模板缺少 id 字段")
    if not VALID_TEMPLATE_ID_RE.fullmatch(template_id):
        raise http_exception_cls(status_code=400, detail="模板 ID 非法")

    validate_template_payload(template_id, template, http_exception_cls)

    root = get_report_templates_root()
    index = read_report_templates_index(http_exception_cls)
    templates = index.get("templates") or {}

    # 检查是否已存在
    action = "overwrite" if template_id in templates else "create"
    conflicts = []
    if template_id in templates:
        conflicts.append(f"模板 {template_id} 已存在，将覆盖")

    # 校验 output_blocks 引用的 block 是否存在
    blocks_dir = root / "blocks"
    for block_id in template.get("output_blocks", []):
        block_path = blocks_dir / f"{block_id}.json"
        if not block_path.exists():
            conflicts.append(f"引用的 block {block_id} 不存在，需手动创建")

    # 写入模板文件
    template_path = safe_template_path(f"{template_id}/template.json", http_exception_cls)
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 更新 index.json
    if action == "create":
        templates[template_id] = {
            "name": template.get("name", template_id),
            "path": f"{template_id}/template.json",
            "enabled": True,
        }
    else:
        templates[template_id]["name"] = template.get("name", template_id)
    index["templates"] = templates
    index_path = root / "index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _invalidate_runtime_template_caches()
    return {"id": template_id, "action": action, "conflicts": conflicts}


def load_route_rules() -> dict:
    """加载所有路由规则。文件不存在时返回空 dict。"""
    root = get_report_templates_root()
    rules_path = root / "route_rules.json"
    if not rules_path.exists():
        return {}
    return json.loads(rules_path.read_text(encoding="utf-8"))


def save_route_rules(template_id: str, keywords: list):
    """保存单个模板的路由规则。

    格式：每个 template_id 一行，值为紧凑 JSON 数组，便于 git diff 追踪。
    """
    root = get_report_templates_root()
    rules_path = root / "route_rules.json"
    rules = {}
    if rules_path.exists():
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rules[template_id] = keywords

    # 每个 key 一行：  "template_id": [[...], [...]],
    lines = ["{"]
    for i, (k, v) in enumerate(rules.items()):
        comma = "," if i < len(rules) - 1 else ""
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}{comma}")
    lines.append("}\n")
    rules_path.write_text("\n".join(lines), encoding="utf-8")
    _invalidate_runtime_template_caches()
