"""长报告文件存储（M1：纯文件；M2 起加 SQLite 元数据表）。

口径（设计 v1.1 §2.3）：
- 一律 `tmp + os.replace` 原子替换，避免阅读页读到半截文件；
- 事件写 `events.jsonl`（M2 换成 deep_report_event 表，SSE 可重放）；
- 列表只回摘要，不把正文带进列表。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.deep_report.models import Outline, ReportMeta, SectionResult

logger = logging.getLogger(__name__)

from agents.deep_report.models import Outline, ReportMeta, SectionResult

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "output" / "deep_reports"

_SUMMARY_KEYS = ("rid", "title", "universe", "template_id", "variant", "status",
                 "stage", "progress", "sections_total", "sections_done", "words",
                 "charts", "created_at", "finished_at", "error")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class DeepReportStore:
    def __init__(self, base: Optional[Path] = None, db=None):
        self.base = Path(base) if base else REPORT_DIR
        self.db = db   # 可选：DeepReportDB，写入时镜像（失败不影响文件）

    # ── 路径 ──
    def dir_of(self, rid: str) -> Path:
        # 防御纵深：rid 会拼进文件路径（即使路由已校验），这里再挡一次路径穿越
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", str(rid or "")):
            raise ValueError(f"非法报告 id: {rid!r}")
        return self.base / rid

    def meta_path(self, rid: str) -> Path:
        return self.dir_of(rid) / "report.json"

    def outline_path(self, rid: str) -> Path:
        return self.dir_of(rid) / "outline.json"

    def section_path(self, rid: str, seq: int, section_id: str) -> Path:
        return self.dir_of(rid) / "sections" / f"{seq:02d}_{section_id}.md"

    # ── 生命周期 ──
    def new_rid(self) -> str:
        return "dr" + uuid.uuid4().hex[:10]

    def create(self, meta: ReportMeta) -> ReportMeta:
        meta.created_at = meta.created_at or _now()
        meta.updated_at = meta.created_at
        self.dir_of(meta.rid).mkdir(parents=True, exist_ok=True)
        self.write_meta(meta)
        return meta

    def write_meta(self, meta: ReportMeta) -> None:
        meta.updated_at = _now()
        _atomic_write(self.meta_path(meta.rid),
                      json.dumps(meta.to_dict(), ensure_ascii=False, indent=2))
        if self.db is not None:
            try:
                self.db.upsert_report(meta.to_dict())
            except Exception:
                logger.warning("DB 镜像报告失败 rid=%s", meta.rid, exc_info=True)

    def update(self, rid: str, **fields: Any) -> ReportMeta:
        meta = self.read_meta(rid)
        for k, v in fields.items():
            if hasattr(meta, k):
                setattr(meta, k, v)
        self.write_meta(meta)
        return meta

    def read_meta(self, rid: str) -> ReportMeta:
        data = json.loads(self.meta_path(rid).read_text(encoding="utf-8"))
        return ReportMeta(**{k: v for k, v in data.items() if k in ReportMeta.__dataclass_fields__})

    def finish(self, rid: str, status: str, **fields: Any) -> ReportMeta:
        return self.update(rid, status=status, finished_at=_now(), **fields)

    # ── 产物 ──
    def save_outline(self, rid: str, outline: Outline) -> None:
        _atomic_write(self.outline_path(rid),
                      json.dumps(outline.to_dict(), ensure_ascii=False, indent=2))

    def load_outline(self, rid: str) -> Optional[Dict[str, Any]]:
        p = self.outline_path(rid)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_section(self, rid: str, result: SectionResult) -> Path:
        path = self.section_path(rid, result.seq, result.section_id)
        body = result.content or f"<!-- 本章失败：{result.error} -->\n"
        _atomic_write(path, body)
        if self.db is not None:
            try:
                self.db.upsert_section(rid, {
                    "seq": result.seq, "section_id": result.section_id,
                    "title": result.title, "status": result.status,
                    "target_words": None, "actual_words": result.words,
                    "error": result.error, "content_path": str(path.relative_to(self.dir_of(rid))),
                })
            except Exception:
                logger.warning("DB 镜像章节失败 rid=%s seq=%s", rid, result.seq, exc_info=True)
        return path

    def assemble(self, rid: str, outline: Outline, sections: List[SectionResult]) -> Path:
        parts = [f"# {outline.title}\n"]
        if outline.thesis:
            parts.append(f"> 投资逻辑：{outline.thesis}\n")
        for r in sorted(sections, key=lambda x: x.seq):
            if r.content:
                parts.append(r.content.rstrip() + "\n")
        md = "\n".join(parts)
        path = self.dir_of(rid) / "report.md"
        _atomic_write(path, md)
        return path

    def append_event(self, rid: str, event: str, payload: Dict[str, Any]) -> None:
        line = json.dumps({"ts": _now(), "event": event, "data": payload}, ensure_ascii=False)
        path = self.dir_of(rid) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.db is not None:
            try:
                self.db.append_event(rid, event, payload)
            except Exception:
                logger.warning("DB 镜像事件失败 rid=%s", rid, exc_info=True)

    # ── 结论卡（M2） ──
    def evidence_dir(self, rid: str) -> Path:
        return self.dir_of(rid) / "evidence"

    def save_evidence(self, rid: str, card: Dict[str, Any]) -> Path:
        ev_id = str(card.get("ev_id") or "ev_000")
        path = self.evidence_dir(rid) / f"{ev_id}.json"
        _atomic_write(path, json.dumps(card, ensure_ascii=False, indent=2))
        if self.db is not None:
            try:
                self.db.upsert_evidence(rid, card)
            except Exception:
                logger.warning("DB 镜像证据失败 rid=%s ev=%s", rid, ev_id, exc_info=True)
        return path

    def load_evidence(self, rid: str, ev_id: str) -> Optional[Dict[str, Any]]:
        p = self.evidence_dir(rid) / f"{ev_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def list_evidence(self, rid: str) -> List[Dict[str, Any]]:
        d = self.evidence_dir(rid)
        if not d.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(d.glob("ev_*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    # ── QA ──
    def save_qa(self, rid: str, payload: Dict[str, Any]) -> Path:
        path = self.dir_of(rid) / "qa.json"
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
        return path

    # ── 列表 ──
    def list_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.base.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.base.glob("*/report.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue   # 坏文件跳过，不返回空壳
            rows.append({k: data.get(k) for k in _SUMMARY_KEYS})
        return rows
