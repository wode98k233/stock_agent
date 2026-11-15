"""长报告正式验收：跑一份 N 节报告并断言关键指标（字数/章节/图表/耗时/成本/trace）。

用法（额度恢复后跑）：
    python scripts/devtools/accept_deep_report.py --base http://127.0.0.1:8031
    python scripts/devtools/accept_deep_report.py --base http://127.0.0.1:8031 --sections 10 --fetch

退出码：0 全过；1 有断言失败（打印表格）。
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，打印 ¥/中文可能 UnicodeEncodeError → 统一 UTF-8（失败则忽略）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TERMINAL = {"done", "partial", "failed", "cancelled"}


def _req(url: str, method="GET", body: dict | None = None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8") or "{}")


def _cost_of_run(rid: str, run_id: str = "") -> tuple[int, float, str]:
    """从 trace 库找该报告 run 的 LLM 调用数与成本。

    ⚠️ 必须用报告自己的 `trace_run_id`：按「最新 deep_report run」取会撞上别的空 run，
    出现「成本 ¥0 / 0 次」的假失败。
    """
    files = sorted(glob.glob(str(ROOT / "db" / "agent_trace" / "agent_trace_*.db")))
    if not files:
        return 0, 0.0, ""
    for path in reversed(files):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            if run_id:
                run = conn.execute("SELECT id, created_at FROM runs WHERE id=?",
                                   (run_id,)).fetchone()
            else:
                run = conn.execute(
                    "SELECT id, created_at FROM runs WHERE agent_name='deep_report' "
                    "ORDER BY rowid DESC LIMIT 1").fetchone()
            if not run:
                continue
            n, cost = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CAST(json_extract(extra,'$.cost.total') AS REAL)),0) "
                "FROM steps WHERE run_id=? AND step_type='llm'", (run["id"],)).fetchone()
            return int(n), float(cost or 0), run["created_at"]
        except Exception:
            continue
        finally:
            conn.close()
    return 0, 0.0, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--universe", default="银行板块")
    ap.add_argument("--request", default="银行板块深度报告：估值、资金面、竞争格局与配置建议")
    ap.add_argument("--template", default="deep_sector_finance")
    ap.add_argument("--sections", type=int, default=10)
    ap.add_argument("--fetch", action="store_true", default=True)
    ap.add_argument("--no-fetch", dest="fetch", action="store_false")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--min-words", type=int, default=10000)
    ap.add_argument("--min-charts", type=int, default=3)
    ap.add_argument("--rid", default="", help="只校验已有报告（不新建、不等待）")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    elapsed = None
    if args.rid:
        rid = args.rid
        _, r = _req(f"{base}/api/deep-reports/{rid}")
        snap = r.get("data") or {}
        print(f"[accept] 校验已有报告 rid={rid} status={snap.get('status')}")
    else:
        t0 = time.time()
        _, resp = _req(f"{base}/api/deep-reports", "POST", {
            "universe": args.universe, "request": args.request,
            "template_id": args.template, "max_sections": args.sections, "fetch": args.fetch})
        rid = resp["data"]["rid"]
        print(f"[accept] rid={rid} 开始（{args.sections} 节，fetch={args.fetch}）")

        snap = {}
        while time.time() - t0 < args.timeout:
            time.sleep(15)
            _, r = _req(f"{base}/api/deep-reports/{rid}")
            snap = r.get("data") or {}
            st = snap.get("status")
            print(f"[accept] {st} progress={snap.get('progress')} "
                  f"章节={snap.get('sections_done')}/{snap.get('sections_total')}")
            if st in TERMINAL:
                break
        elapsed = time.time() - t0

    html_ok = False
    try:
        with urllib.request.urlopen(f"{base}/api/deep-reports/{rid}/html", timeout=15) as r:
            html_ok = r.status == 200 and b"<html" in r.read(4096).lower()
    except Exception:
        html_ok = False

    calls, cost, run_at = _cost_of_run(rid, snap.get("trace_run_id") or "")
    min_sections = min(8, args.sections)   # 冒烟（1–2 节）按实际节数；正式验收默认 ≥8
    rows = [
        ("状态", snap.get("status") in ("done", "partial"), snap.get("status")),
        ("字数", (snap.get("words") or 0) >= args.min_words,
         f"{snap.get('words')}（门槛 {args.min_words}）"),
        ("章节", snap.get("sections_done") == snap.get("sections_total")
         and (snap.get("sections_total") or 0) >= min_sections,
         f"{snap.get('sections_done')}/{snap.get('sections_total')}"),
        ("图表", (snap.get("charts") or 0) >= args.min_charts,
         f"{snap.get('charts')}（门槛 {args.min_charts}）"),
        ("耗时", elapsed is None or elapsed <= args.timeout,
         "跳过（复用已有报告）" if elapsed is None else f"{elapsed / 60:.1f} 分钟"),
        ("阅读页", html_ok, "200" if html_ok else "不可访问"),
        ("成本", cost > 0, f"¥{cost:.4f} / {calls} 次 LLM"),
    ]
    print("\n[accept] 结果")
    ok_all = True
    for name, ok, detail in rows:
        ok_all &= bool(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:6s} {detail}")
    print(f"\n[accept] {'全部通过' if ok_all else '有失败项'}  rid={rid}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
