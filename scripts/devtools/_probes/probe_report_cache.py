"""探测：中转链路上「模板派生的静态 system 前缀」能否被缓存

背景（PRD-A9 §13 未决项）：
  report-fast 的 system 完全由模板 JSON 生成（两次实测前缀指纹相同 e5bf005c），
  但两次 cached 都是 0。要分清是「TTL 太短」还是「前缀里还有变量」。

关键背景：主模型走 https://opencode.ai/zen/go/v1 中转，**不是 DeepSeek 官方 API**，
所以 DeepSeek 官方的「磁盘缓存落盘规则」不适用，只能按返回的 usage 实测。

用法：
  python scripts/devtools/probe_report_cache.py both 0   # 同进程连续两发（基本机制）
  python scripts/devtools/probe_report_cache.py warm     # 进程 A：第 1 发
  #   等 N 秒
  python scripts/devtools/probe_report_cache.py probe    # 进程 B：第 2 发（跨进程磁盘缓存）
"""
import hashlib
import sys
import time
from pathlib import Path

# 根锚点统一按项目约定（AGENTS.md §脚本与运维）：脚本在 scripts/devtools/_probes/，上溯三级
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agents.analysis.template_store import load_template
from agents.analysis.report_builder import _build_prompt
from utils.llm_factory import get_report_llm

USER_A = "请分析贵州茅台(600519)：近5日主力资金净流入多少？"
USER_B = "请分析宁德时代(300750)：最近20个交易日北向资金持股变化如何？"
USER_C = "请分析比亚迪(002594)：当前估值分位处于什么水平？"


def _usage_of(resp):
    """三处都翻一遍，兼容中转层的字段摆放。"""
    srcs = [
        (getattr(resp, "usage_metadata", None) or {}),
        ((getattr(resp, "response_metadata", None) or {}).get("token_usage") or {}),
        (getattr(resp, "llm_output", None) or {}).get("token_usage") or {},
    ]
    for u in srcs:
        if not u:
            continue
        details = u.get("prompt_tokens_details") or u.get("input_token_details") or {}
        hit = details.get("cached_tokens") or details.get("cache_read") or 0
        total = u.get("prompt_tokens") or u.get("input_tokens") or 0
        if total:
            return int(total), int(hit), u.get("prompt_cache_hit_tokens") or 0, \
                u.get("prompt_cache_miss_tokens") or 0
    return 0, 0, 0, 0


def build_system():
    """按 build() 真实路径构造 report-fast 的 system（slot 为空、raw 短 → 路径 B）。"""
    tpl = load_template("default")
    msgs = _build_prompt(tpl, {}, "x", raw_result="", tool_evidence="")
    parts = []
    for m in msgs:
        role = m[0] if isinstance(m, tuple) else getattr(m, "type", "")
        if role != "system":
            break
        parts.append(m[1] if isinstance(m, tuple) else m.content)
    return "\n\n".join(parts), len(msgs)


def probe(label, system, user):
    llm = get_report_llm()
    t0 = time.time()
    resp = llm.invoke([("system", system), ("user", user)])
    dt = time.time() - t0
    total, hit, ds_hit, ds_miss = _usage_of(resp)
    pct = hit / total * 100 if total else 0.0
    fp = hashlib.sha256(system.encode()).hexdigest()[:16]
    print(f"[{label}]")
    print(f"    system   = {len(system)} 字符 ≈ {len(system)//3} tok   fp={fp}")
    print(f"    prompt   = {total} tok   cached = {hit} ({pct:.1f}%)"
          + (f"   deepseek_hit={ds_hit} miss={ds_miss}" if ds_hit or ds_miss else ""))
    print(f"    耗时     = {dt:.1f}s")
    return hit, total


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

    system, nmsg = build_system()
    llm = get_report_llm()
    print(f"base_url = {getattr(llm, 'openai_api_base', '?')}")
    print(f"model    = {getattr(llm, 'model_name', '?')}")
    print(f"system   = {len(system)} 字符 ≈ {len(system)//3} tok   messages 总数={nmsg}")
    print()

    if mode == "both":
        probe("同进程 第1发", system, USER_A)
        time.sleep(delay)
        probe("同进程 第2发", system, USER_B)
        time.sleep(delay)
        probe("同进程 第3发", system, USER_C)
    else:
        who = {"warm": USER_A, "probe": USER_B}[mode]
        probe(f"进程 {mode}", system, who)


if __name__ == "__main__":
    main()