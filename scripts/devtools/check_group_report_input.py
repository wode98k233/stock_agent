"""Group 报告层「有没有拿到原始数据」契约自检。

存在理由（为什么需要这个脚本）：
    2026-10-01 agent_group 事故（logs/2026-10-01/…-19-10-d557f47f….log）里，
    报告写作节点最终**一份原始数据都没拿到**——`plan_steps[0].result` 被失败的补采
    覆盖成 0 字符，而 `report_builder` 又因 `if step_results / else raw_result`
    把含数据底版的 `raw_result` 整段丢弃。结果报告只能改写 4 份散文，
    把「深成最大回撤 −7.31%」写成「深成月涨跌幅」（底版实为 −8.044%）。
    这类劣化在日志里**没有任何 ERROR/WARNING**，靠翻日志才发现得了。
    本脚本把它变成一条可自动执行的断言。

检查项（任一失败即退出码 1）：
    C1 存在 report-fast 输入块（报告层确实被调用）
    C2 报告层存在「原始工具数据」或「原始素材」消息（数据通道打开）
    C3 报告层不存在"只剩任务描述"的空步骤（`### Step N …` 段正文为空）
    C4 若存在「原始素材」，其正文长度 > 0
    C5 模板路由结果不是 theme_trading（当用户问题是"市场复盘"句式时）

用法：
    python scripts/devtools/check_group_report_input.py                 # 自动取最新日志
    python scripts/devtools/check_group_report_input.py <logfile>
    python scripts/devtools/check_group_report_input.py --dir logs/2026-10-01
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_REC = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} │")
_MSG = re.compile(r"\[L\] req=\S+\s+\[(\d+)\] (\w+) │ content=(.*)$")
_HDR = re.compile(r"\[L\] req=\S+ ═══ LLM 输入 \[(.*?)\] ═══")
_STEP_HDR = re.compile(r"### Step (\d+) \[(.*?)\]: (.*)$", re.S)

# 报告层认为"有数据可用"的最小正文长度（空步骤实测为 0，正常 4k~6k）
_MIN_STEP_BODY = 200


def _load_messages(path: str) -> list[tuple[int, str]]:
    """返回 [(header_line_index, label)] —— 每个 LLM 输入块的表头位置与标签。"""
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    out = []
    for i, ln in enumerate(lines):
        m = _HDR.search(ln)
        if m:
            out.append((i, m.group(1)))
    return out


def _block(lines: list[str], start: int) -> list[str]:
    """取一个 LLM 输入块的完整内容（含续行），止于下一条非 _log_input_messages 记录。"""
    j = start + 1
    while j < len(lines):
        if _REC.match(lines[j]) and "_log_input_messages" not in lines[j]:
            break
        j += 1
    return lines[start:j]


def _parse_messages(block: list[str]) -> list[tuple[int, str, str]]:
    """块内容 → [(序号, 角色, 正文)]"""
    out, cur = [], None
    for ln in block[1:]:
        m = _MSG.search(ln)
        if m:
            if cur:
                out.append(cur)
            cur = (int(m.group(1)), m.group(2), m.group(3))
        elif cur is not None:
            cur = (cur[0], cur[1], cur[2] + "\n" + ln)
    if cur:
        out.append(cur)
    return out


def check(path: str) -> list[tuple[str, bool, str]]:
    raw = open(path, encoding="utf-8", errors="replace").read()
    lines = raw.split("\n")
    results: list[tuple[str, bool, str]] = []

    # 定位报告层输入块（label == report-fast）
    report_blocks = [(i, lab) for i, lab in _load_messages(path) if lab == "report-fast"]
    results.append(("C1 report-fast 输入块存在", bool(report_blocks),
                    f"找到 {len(report_blocks)} 个"))
    if not report_blocks:
        return results

    msgs = _parse_messages(_block(lines, report_blocks[-1][0]))

    # C2：必须有一条**以通道标题开头**的消息。
    # 不能用子串匹配——要求文案里本来就写着「引用【原始工具数据 / 原始素材】中的数字」，
    # 早先按子串判会误报为 PASS（已踩过）。
    data_msg = next((m for m in msgs
                     if m[2].startswith("## 原始工具数据") or m[2].startswith("## 原始素材")), None)
    results.append(("C2 存在原始数据通道消息", data_msg is not None,
                    f"{data_msg[2][:40]!r} 正文 {len(data_msg[2])} 字符" if data_msg
                    else "没有任何以「## 原始工具数据 / ## 原始素材」开头的消息 → 报告层零原始数据"))

    # C3：空步骤。格式为 `### Step N [skill]: {purpose}\n{result}`，
    # 因此要判**第一个换行之后**的正文，而不是整条消息长度
    # （任务描述本身就有 ~250 字符，按总长度判会漏掉事故形态）。
    empty_steps = []
    for i, role, content in msgs:
        if role != "ai":
            continue
        head, sep, body = content.strip().partition("\n")
        m = _STEP_HDR.match(head)
        if m and len(body.strip()) < _MIN_STEP_BODY:
            empty_steps.append(f"Step {m.group(1)} [{m.group(2)}] body={len(body.strip())}")
    results.append(("C3 无「只剩任务描述」的空步骤", not empty_steps,
                    "、".join(empty_steps) if empty_steps else f"所有 Step 正文 ≥ {_MIN_STEP_BODY} 字符"))

    # C4：原始素材正文非空（走的是保底通道时才检查）
    raw_material_len = None
    for i, role, content in msgs:
        if content.startswith("## 原始素材"):
            _, _, body = content.partition("\n")
            raw_material_len = len(body.strip())
    results.append(("C4 原始素材正文非空", raw_material_len is None or raw_material_len > 0,
                    "未走保底通道（由 C2 的条目通道覆盖）" if raw_material_len is None
                    else f"正文 {raw_material_len} 字符"))

    # C5：市场复盘句式不应路由到 theme_trading
    m = re.search(r"选中报告模板:\s*(\S+)", raw)
    if m:
        tid = m.group(1)
        asked_review = ("总结" in raw[:4000] and "市场" in raw[:4000])
        ok = not (asked_review and tid == "theme_trading")
        results.append(("C5 模板路由未被「题材」二字带偏", ok, f"选中 {tid}"))
    return results


def _pick_log(args) -> str:
    if args.logfile:
        return args.logfile
    base = args.dir or os.path.join(_ROOT, "logs")
    cands = sorted(glob.glob(os.path.join(base, "**", "*.log"), recursive=True),
                   key=os.path.getmtime, reverse=True)
    if not cands:
        raise SystemExit(f"未找到日志: {base}")
    return cands[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Group 报告层原始数据契约自检")
    ap.add_argument("logfile", nargs="?", help="日志文件路径")
    ap.add_argument("--dir", help="日志目录（取最新）")
    args = ap.parse_args()

    path = _pick_log(args)
    print(f"日志: {path}\n")
    results = check(path)
    failed = 0
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} —— {detail}")
        failed += 0 if ok else 1
    print()
    print(f"结论: {len(results) - failed}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
