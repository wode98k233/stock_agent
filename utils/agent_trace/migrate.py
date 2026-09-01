import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from utils.agent_trace.db_adapter import SQLiteAdapter

logger = logging.getLogger(__name__)

_EXPORT_VERSION = "1.0"


class TraceExporter:
    def __init__(self, db_path: str = ""):
        self._db = SQLiteAdapter(db_path) if db_path else None

    def _get_adapter(self, db_path: str = "") -> SQLiteAdapter:
        if self._db is not None:
            return self._db
        return SQLiteAdapter(db_path)

    def export_run(self, run_id: str, output_path: str | None = None,
                   db_path: str = "") -> str:
        adapter = self._get_adapter(db_path)
        run = adapter.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} 不存在")
        steps = adapter.get_steps_with_messages(run_id)
        data = {
            "version": _EXPORT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "run": run,
            "steps": steps,
        }
        out = output_path or f"trace_{run_id[:8]}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        return out

    def export_batch(self, run_ids: list[str] | None = None,
                     status_filter: str = "", limit: int = 100,
                     output_dir: str = "exported_traces",
                     db_path: str = "") -> list[str]:
        adapter = self._get_adapter(db_path)
        if run_ids is None:
            runs = adapter.get_runs(limit=limit, status=status_filter)
            run_ids = [r["id"] for r in runs]
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for rid in run_ids:
            try:
                p = self.export_run(rid, str(out_dir / f"trace_{rid[:8]}.json"), db_path=db_path)
                results.append(p)
            except Exception as e:
                logger.warning("导出 run %s 失败: %s", rid, e)
        return results


class TraceImporter:
    def __init__(self, db_path: str = ""):
        self._db_path = db_path

    def _get_adapter(self) -> SQLiteAdapter:
        return SQLiteAdapter(self._db_path)

    def import_run(self, json_path: str,
                   strategy: Literal["skip", "replace", "error"] = "skip",
                   db_path: str = "") -> bool:
        if json_path == "-":
            data = json.load(sys.stdin)
        else:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        if data.get("version") != _EXPORT_VERSION:
            logger.warning("导入版本不匹配: %s (期望 %s)", data.get("version"), _EXPORT_VERSION)

        actual_db = db_path or self._db_path
        adapter = SQLiteAdapter(actual_db)
        run = data["run"]
        run_id = run["id"]

        existing = adapter.get_run(run_id)
        if existing:
            if strategy == "skip":
                logger.info("Run %s 已存在，跳过", run_id)
                adapter.close()
                return False
            elif strategy == "replace":
                adapter.delete_run(run_id)
            elif strategy == "error":
                adapter.close()
                raise ValueError(f"Run {run_id} 已存在")

        adapter.insert_run(run)
        if run.get("status") != "running":
            adapter.update_run(run_id,
                               output=run.get("output"),
                               status=run.get("status", "success"),
                               error=run.get("error"),
                               finished_at=run.get("finished_at"),
                               duration_ms=run.get("duration_ms"))

        for step in data.get("steps", []):
            messages = step.pop("messages", [])
            sid = adapter.insert_step(step)
            if messages:
                for m in messages:
                    m["step_id"] = sid
                adapter.insert_messages(messages)
            if step.get("status") != "running":
                adapter.update_step(sid,
                                    output=step.get("output"),
                                    status=step.get("status", "success"),
                                    error=step.get("error"),
                                    finished_at=step.get("finished_at"),
                                    duration_ms=step.get("duration_ms"))

        adapter.close()
        return True

    def import_batch(self, json_dir: str,
                     strategy: Literal["skip", "replace", "error"] = "skip",
                     db_path: str = "") -> dict:
        p = Path(json_dir)
        json_files = sorted(p.glob("*.json"))
        imported = 0
        skipped = 0
        errors = 0
        for jf in json_files:
            try:
                ok = self.import_run(str(jf), strategy, db_path=db_path)
                if ok:
                    imported += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error("导入 %s 失败: %s", jf, e)
                errors += 1
        return {"imported": imported, "skipped": skipped, "errors": errors}
