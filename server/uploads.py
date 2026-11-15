"""对话图片上传：落盘、读取、引用扫描与清理。

v1 只做「图片 → 文字」：原图落盘供前端展示，模型侧只吃转述文字。
目录：`<app>/output/uploads/<YYYYMM>/<id>.<ext>`（output/ 已 gitignore）。
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from utils.app_paths import get_db_path, get_uploads_dir

_ALLOWED = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
_MAGIC = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".webp": b"RIFF"}
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _root() -> Path:
    return Path(get_uploads_dir())


def _max_bytes() -> int:
    return max(1, int(getattr(Config, "UPLOAD_MAX_MB", 5))) * 1024 * 1024


def save_image(data_b64: str, mime: str, name: str = "") -> Dict[str, Any]:
    """校验并落盘一张图；返回 {id, name, mime, size, url}。非法输入抛 ValueError。"""
    mime = (mime or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED:
        raise ValueError("仅支持 PNG / JPEG / WebP 图片")
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except Exception:  # noqa: BLE001
        raise ValueError("图片数据不是合法的 base64")
    if not raw:
        raise ValueError("图片为空")
    if len(raw) > _max_bytes():
        raise ValueError(f"图片超过 {getattr(Config, 'UPLOAD_MAX_MB', 5)}MB 上限")
    ext = _ALLOWED[mime]
    if not raw.startswith(_MAGIC[ext]):
        raise ValueError("图片内容与声明类型不符")
    att_id = uuid.uuid4().hex[:16]
    folder = _root() / datetime.now().strftime("%Y%m")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{att_id}{ext}"
    path.write_bytes(raw)
    return {"id": att_id, "name": name or path.name, "mime": mime,
            "size": len(raw), "url": f"/api/uploads/{att_id}"}


def find_file(att_id: str) -> Optional[Path]:
    """按 id 找文件（只认 16 位 hex，防目录穿越）。"""
    if not _ID_RE.match(str(att_id or "")):
        return None
    for path in _root().glob(f"*/{att_id}.*"):
        if path.is_file():
            return path
    return None


def read_image(att_id: str) -> Optional[tuple[bytes, str]]:
    path = find_file(att_id)
    if not path:
        return None
    mime = _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
    return path.read_bytes(), mime


def delete_ids(ids: List[str]) -> int:
    """删除指定图片文件（对话删除时联动用）；返回删除个数。"""
    removed = 0
    for att_id in ids or []:
        path = find_file(att_id)
        if not path:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def referenced_ids() -> set:
    """从 web_messages.attachments 里收集仍被引用的图片 id（只读）。"""
    out: set = set()
    try:
        con = sqlite3.connect(f"file:{Path(get_db_path()).as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT attachments FROM web_messages "
                "WHERE attachments IS NOT NULL AND attachments != ''").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return out
    for (raw,) in rows:
        try:
            for item in json.loads(raw or "[]"):
                if isinstance(item, dict) and item.get("id"):
                    out.add(str(item["id"]))
        except Exception:  # noqa: BLE001
            continue
    return out


def cleanup(*, retention_days: Optional[int] = None,
            max_total_mb: Optional[int] = None, dry_run: bool = False) -> Dict[str, Any]:
    """清理孤儿/过期/超量图片；返回统计。

    - 孤儿：DB 里没有引用，且落盘超过 1 天（避免误删刚上传未发送的）
    - 过期：被引用但超过 retention_days（默认 45 天）
    - 超量：目录超过 max_total_mb（默认 500MB）时按最老 mtime 删到 80%
    """
    retention = int(retention_days or getattr(Config, "UPLOAD_RETENTION_DAYS", 45))
    cap = int(max_total_mb or getattr(Config, "UPLOAD_MAX_TOTAL_MB", 500)) * 1024 * 1024
    refs = referenced_ids()
    now = time.time()
    files = [p for p in _root().glob("*/*") if p.is_file()]
    sizes = {p: p.stat().st_size for p in files}
    total = sum(sizes.values())

    delete: List[Path] = []
    for path in files:
        att_id = path.stem
        if not _ID_RE.match(att_id):
            continue
        mtime = path.stat().st_mtime
        if att_id not in refs:
            if now - mtime > 86400:
                delete.append(path)
        elif now - mtime > retention * 86400:
            delete.append(path)

    freed = sum(sizes[p] for p in delete)
    if total - freed > cap:
        remain = sorted((p for p in files if p not in delete), key=lambda p: p.stat().st_mtime)
        target = cap * 0.8
        current = total - freed
        for path in remain:
            if current <= target:
                break
            delete.append(path)
            current -= sizes[path]
            freed += sizes[path]

    if not dry_run:
        for path in delete:
            try:
                path.unlink()
            except OSError:
                pass
    return {"deleted": len(delete), "freed_bytes": freed,
            "kept": len(files) - len(delete), "total_bytes": total,
            "retention_days": retention, "max_total_mb": cap // (1024 * 1024),
            "dry_run": dry_run}
