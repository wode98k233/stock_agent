"""策略会话端到端冒烟：像真人一样「开对话 → 说想法 → 答澄清卡 → 起跑 → 看结果」。

为什么要有它：策略研发这条链路（闸门 → 澄清槽位 → 会话执行器 → 研发循环 → 结果卡）
是**对话式**的，单测只能锁住各段契约，串起来是否顺畅必须真跑几轮才知道。
本脚本用真 HTTP + 真 SSE 驱动，不 mock 任何一层。

用法：
    venv/Scripts/python.exe scripts/devtools/strategy_session_smoke.py                 # 跑全部想法
    venv/Scripts/python.exe scripts/devtools/strategy_session_smoke.py --only 1,3      # 只跑第 1、3 条
    venv/Scripts/python.exe scripts/devtools/strategy_session_smoke.py --list          # 只看清单

产物：logs/strategy_smoke_<时间戳>.json（每轮的想法 / 澄清问答 / 助手回复 / 研发结果）。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


# 想法清单：刻意覆盖不同表达方式与不同引擎能力（纯时序 / 指标交叉 / 多条件 / 量价 / 振荡指标）
IDEAS = [
    dict(idea="尾盘买入，第二天开盘卖出，标的 600519，跑两年",
         entry="尾盘价买入", exit="次日开盘卖出", period="720",
         why="纯时序：无指标条件，走 hold_bars + same_close/next_open 口径"),
    dict(idea="5 日均线上穿 20 日均线买入，下穿卖出，600519，两年",
         entry="5 日均线上穿 20 日均线", exit="5 日均线下穿 20 日均线", period="720",
         why="经典指标交叉（cross_above/cross_below）"),
    dict(idea="MACD 金叉买入，收盘价跌破 10 日均线卖出，600519，两年",
         entry="MACD 金叉", exit="收盘价跌破 10 日均线", period="720",
         why="多指标 + 单边条件（买点交叉、卖点比较）"),
    dict(idea="收盘价创 20 日新高且成交量大于 20 日均量的 1.5 倍时买入，持有 5 根后卖出，600519，两年",
         entry="收盘价创 20 日新高且成交量大于 20 日均量 1.5 倍", exit="持有 5 根后卖出", period="720",
         why="量价组合 + 纯时序卖点（考察可表达性闸门与 volume 指标）"),
    dict(idea="RSI 低于 30 买入，高于 70 卖出，600519，两年",
         entry="RSI(14) 低于 30", exit="RSI(14) 高于 70", period="720",
         why="振荡指标 + 阈值比较"),
    dict(idea="KDJ 金叉且收盘价站上布林中轨买入，KDJ 死叉卖出，600519，两年",
         entry="KDJ 金叉且收盘价站上布林中轨", exit="KDJ 死叉", period="720",
         why="多指标与逻辑与（AND）组合"),
]


def _post(base, path, payload=None, timeout=60):
    res = requests.post(base + path, json=payload or {}, timeout=timeout)
    res.raise_for_status()
    return res.json() if res.text else {}


def _get(base, path, timeout=60):
    res = requests.get(base + path, timeout=timeout)
    res.raise_for_status()
    return res.json() if res.text else {}


def _stream(base, task_id, on_event, timeout=900):
    """读 SSE 直到 final / error / cancelled。返回结束原因。"""
    url = base + "/api/tasks/" + task_id + "/events"
    ended = "timeout"
    t0 = time.time()
    with requests.get(url, stream=True, timeout=(10, timeout)) as res:
        res.raise_for_status()
        event_name = ""
        for raw in res.iter_lines(decode_unicode=True):
            if time.time() - t0 > timeout:
                ended = "timeout"
                break
            if raw is None:
                continue
            line = raw.strip()
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            body = line.split(":", 1)[1].strip()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            on_event(event_name, data)
            if event_name in ("final", "task_error", "task_cancelled"):
                ended = event_name
                break
    return ended


def _one_turn(base, dialog_uuid, item, out, turn):
    """发一条消息 → 读 SSE → 澄清卡一到就作答（不能等流结束，否则干等 5 分钟超时）
    → 记录助手回复与研发 run_uuid。"""
    sent = _post(base, "/api/dialogs/" + dialog_uuid + "/messages",
                 {"content": item["idea"], "mode": "react_stock"})
    task_id = sent.get("task_id") or sent.get("id") or ""
    print("  第" + str(turn) + "轮 task: " + str(task_id))
    state = {"clarify": None, "reply": "", "progress": 0, "run_uuid": "", "answered": False}
    answers = {"universe": "600519", "entry": item.get("entry") or item["idea"],
               "exit": item.get("exit") or "次日开盘卖出", "period": item.get("period") or "720"}

    def on_event(name, data):
        if name == "clarify_required":
            state["clarify"] = data
            # ⚠️ 必须**当场**作答：这张卡会阻塞 worker 线程直到超时（默认 300s），
            # 等 SSE 流结束再答等于每个想法白等 5 分钟，而且拿到的只是超时兜底文案。
            if data.get("reason") == "strategy_slots" and not state["answered"]:
                state["answered"] = True
                print("  收到澄清卡 → 立刻作答: " + json.dumps(answers, ensure_ascii=False))
                try:
                    _post(base, "/api/tasks/" + task_id + "/clarify",
                          {"answers": answers, "skipped": []})
                except Exception as exc:      # noqa: BLE001
                    print("  !! 作答失败: " + str(exc))
        elif name == "progress":
            state["progress"] += 1
        elif name == "final":
            state["reply"] = data.get("content") or ""
        elif name in ("task_error", "task_cancelled"):
            state["reply"] = (data.get("message") or name)

    ended = _stream(base, task_id, on_event)
    print("  第" + str(turn) + "轮 SSE 结束: " + ended + " · 进度事件 " + str(state["progress"]))
    if state["answered"]:
        out["clarify"] = {"asked": True, "answers": answers}
    out["turns"].append({"turn": turn, "ended": ended, "reply": state["reply"][:400],
                         "clarify": bool(state["clarify"])})
    reply = state["reply"] or ""
    for token in reply.replace("**", " ").replace("：", " ").split():
        if token.startswith("SR-"):
            state["run_uuid"] = token.strip("，。、（）()").strip(chr(96))
            break
    if state["run_uuid"]:
        out["research"] = {"run_uuid": state["run_uuid"]}
    return {"turns": out["turns"], "clarify": out["clarify"], "research": out.get("research")}


def _wait_research(base, run_uuid, timeout_s):
    """轮询研发 run 直到结束（done/failed/cancelled），返回关键结论。"""
    print("  研发 " + run_uuid + " 跑起来了，等结果（最多 " + str(timeout_s) + "s）…")
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        try:
            # ⚠️ 接口是 {code, data:{run, iters, best_card}} 信封：状态在 data.run 里。
            # 早期直接读顶层 detail["status"] 拿到 None → 每个想法白等满 30 分钟。
            payload = _get(base, "/api/strategy-research/runs/" + run_uuid)
        except Exception as exc:                  # noqa: BLE001
            print("    轮询失败: " + str(exc))
            time.sleep(10)
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        run = (data or {}).get("run") or {}
        last = {**run, "iters": (data or {}).get("iters") or [],
                "best_card": (data or {}).get("best_card")}
        status = str(run.get("status") or "")
        iters = last.get("iters") or []
        if status and status not in ("running", "pending", "queued", ""):
            print("  研发结束: status=" + status + " 轮数=" + str(len(iters) if isinstance(iters, list) else iters)
                  + " 用时 " + str(int(time.time() - t0)) + "s")
            break
        time.sleep(15)
    best = last.get("best_card") or {}
    iters = last.get("iters") or []
    failed = [it for it in iters if isinstance(it, dict) and it.get("compile_error")]
    return {
        "run_uuid": run_uuid,
        "status": last.get("status"),
        "stop_reason": last.get("stop_reason"),
        "iters": len(iters) if isinstance(iters, list) else None,
        "best": ({k: best.get(k) for k in ("round", "hypothesis", "verdict", "oos_metrics", "oos_sharpe", "compile_error")}
                 if isinstance(best, dict) else best),
        "compile_errors": [str(it.get("compile_error"))[:160] for it in failed][:3],
        "raw_keys": sorted(last.keys())[:20],
    }


def run_idea(base, item, idx, wait_research=1800):
    out = {"index": idx, "idea": item["idea"], "why": item["why"],
           "dialog_uuid": "", "turns": [], "clarify": None, "research": None, "errors": []}
    print("")
    print("===== [" + str(idx) + "] " + item["idea"] + " =====")
    try:
        dlg = _post(base, "/api/dialogs", {"title": "[冒烟" + str(idx) + "] " + item["idea"][:24],
                                           "mode": "react_stock"})
        dialog_uuid = dlg.get("dialog_uuid") or dlg.get("uuid") or ""
        out["dialog_uuid"] = dialog_uuid
        print("对话: " + dialog_uuid)
        out.update(_one_turn(base, dialog_uuid, item, out, turn=1))
        if out["turns"] and "开始" in (out["turns"][-1].get("reply") or "") and not out["research"]:
            out.update(_one_turn(base, dialog_uuid, {"idea": "开始"}, out, turn=2))
        run_uuid = (out.get("research") or {}).get("run_uuid")
        if run_uuid:
            out["research"] = _wait_research(base, run_uuid, wait_research)
    except Exception as exc:                     # noqa: BLE001 - 冒烟脚本要如实记录失败
        out["errors"].append(type(exc).__name__ + ": " + str(exc))
        print("!! 异常: " + str(exc))
    print("结果: " + json.dumps({k: out[k] for k in ("clarify", "research", "errors")},
                                ensure_ascii=False)[:600])
    return out


def main():
    ap = argparse.ArgumentParser(description="策略会话端到端冒烟")
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--wait", type=int, default=1800, help="单个研发 run 的最长等待秒数")
    args = ap.parse_args()

    if args.list:
        for i, it in enumerate(IDEAS, 1):
            print(str(i) + ". " + it["idea"])
            print("   → " + it["why"])
        return 0

    picked = list(range(1, len(IDEAS) + 1))
    if args.only:
        picked = [int(x) for x in args.only.split(",") if x.strip()]

    health = _get(args.base, "/api/health")
    print("服务: " + str(health.get("status")) + " · agent_ready = " + str(health.get("agent_ready")))

    results = []
    for i in picked:
        results.append(run_idea(args.base, IDEAS[i - 1], i, wait_research=args.wait))

    out_path = ROOT / "logs" / ("strategy_smoke_" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print("===== 汇总 =====")
    for r in results:
        res = r.get("research") or {}
        best = res.get("best") if isinstance(res.get("best"), dict) else {}
        print("[" + str(r["index"]) + "] " + r["idea"][:34] + " → status=" + str(res.get("status"))
              + " 轮数=" + str(res.get("iters")) + " 最优第" + str(best.get("round")) + "轮"
              + " 澄清=" + ("有" if r.get("clarify") else "无") + " 错误=" + str(r["errors"] or "无"))
    print("明细: " + str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
