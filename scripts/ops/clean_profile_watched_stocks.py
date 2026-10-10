"""整理 `data/user_profile/*.json`：剔除 `watched_stocks` / `recent_queries` 里的污染键。

背景（2026-10-01）
------------------
技术面图板把「市场日报里的 000001（上证指数）」误当个股（已在代码侧修复），
而画像抽取器同期把同一批文本切碎写进了 `watched_stocks`：

    "000001": 5.08        ← 市场报告里的上证指数码，权重是其他标的的 4~6 倍
    "包括M" / "均线M" / "市盈率T" / "创新药E"   ← 中文长句/长名被截断的碎片
    "还有牧原" / "其次看牧原"                    ← 动词连词前缀
    "恒生" / "无股息率"                          ← 截断词，本地库里不存在该标的

画像的「关注个股」会回灌后续 prompt 与 K 线保活范围 → **错误标签会自我强化**
（`000001` 已在 `stock_daily` 里积累成平安银行的 433 根日线）。

剔除规则（逐条可审计；命中即剔除，理由写入 `repaired_from`）
------------------------------------------------------------
R1 `--drop-codes` 显式指定（默认 `000001`：歧义码污染，用户持仓/提问均无平安银行）
R2 含拉丁字符且不是合法 6 位码 → 截断碎片
R3 不满足 `memory.user_profile._looks_like_stock_name` → 句子切片
R4 非 6 位码，且本地 `stock_info`/`index_info` 都没有这个名称 → 库里不存在的"标的"

R6 `recent_queries` 里的测试/失败条目（`测试问题` 之类，或 summary 是空壳失败文案）

**刻意不做**的检查（会误杀）：曾用「6 位码必须存在于本地 `stock_info`」兜底，
结果 601088 / 601225 / 600859 / 300498 这些**合法但未同步进本地库**的代码被误判
——本地库有无与代码是否合法无关，该检查已降级为「仅提示」（`watched_codes_not_local`）。

**安全护栏**：持仓（`holdings[].stock_code`）涉及的键永不剔除；`--keep` 可追加白名单。

用法：
    python scripts/ops/clean_profile_watched_stocks.py                # 预演（默认，只报告）
    python scripts/ops/clean_profile_watched_stocks.py --apply        # 实际写入（自动备份）
    python scripts/ops/clean_profile_watched_stocks.py --keep 000001  # 保留某些键
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from memory.user_profile import _looks_like_stock_name  # noqa: E402
from utils.app_paths import get_user_profile_dir  # noqa: E402

_CODE6_RE = re.compile(r"\d{6}")
_ASCII_RE = re.compile(r"[A-Za-z0-9]")
_DEFAULT_DROP_CODES = ("000001",)
_JUNK_QUERIES = {"测试问题", "测试", "test", "TEST"}
_JUNK_SUMMARY_MAX_CHARS = 60          # 失败/超限的空壳摘要
_JUNK_SUMMARY_KEYS = ("预算超限", "分析因达到迭代上限提前结束")


def _load_known_symbols() -> tuple[set[str], set[str]]:
    """本地库已知的代码/名称（名称用于 R4、代码用于 R5）。"""
    from config import Config
    names, codes = set(), set()
    for path in (Config.get_market_data_db_path(),):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            for table in ("stock_info", "index_info"):
                try:
                    for code, name in conn.execute(f"SELECT code, name FROM {table}"):
                        codes.add(str(code))
                        if name:
                            names.add(str(name))
                except Exception:
                    continue
        finally:
            conn.close()
    return names, codes


def _pollution_reason(key: str, names: set[str], codes: set[str],
                      drop_codes: set[str]) -> str | None:
    """返回剔除理由；None 表示保留。

    注意：**不校验 6 位码是否存在于本地库**——本地库只覆盖已同步的标的，
    601088/601225/600859/300498 这类合法代码不在库里属正常（会误杀）。
    """
    if key in drop_codes:
        return "R1 显式指定（歧义码污染）"
    if _CODE6_RE.fullmatch(key):
        return None
    if _ASCII_RE.search(key):
        return "R2 含拉丁字符的非 6 位码键（截断碎片）"
    if not _looks_like_stock_name(key):
        return "R3 不满足 _looks_like_stock_name（句子切片）"
    if key not in names:
        return "R4 本地 stock_info/index_info 无此名称"
    return None


def _is_junk_query(item: dict) -> bool:
    q = str(item.get("query") or "").strip()
    s = str(item.get("summary") or "").strip()
    if q in _JUNK_QUERIES:
        return True
    return len(s) <= _JUNK_SUMMARY_MAX_CHARS and any(k in s for k in _JUNK_SUMMARY_KEYS)


def clean_file(path: Path, *, dry_run: bool, drop_codes: set[str],
               keep: set[str]) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  跳过（无法解析）: {path.name}: {e}")
        return {}

    names, codes = _load_known_symbols()
    held = {str(h.get("stock_code")) for h in (data.get("holdings") or [])
            if isinstance(h, dict) and h.get("stock_code")}

    kept, dropped = {}, {}
    for key, weight in (data.get("watched_stocks") or {}).items():
        if key in keep or key in held:
            kept[key] = weight
            continue
        reason = _pollution_reason(key, names, codes, drop_codes)
        if reason:
            dropped[key] = (weight, reason)
        else:
            kept[key] = weight

    recent = data.get("recent_queries") or []
    kept_recent = [q for q in recent if not _is_junk_query(q)]
    dropped_recent = len(recent) - len(kept_recent)
    not_local = sorted(k for k in kept if _CODE6_RE.fullmatch(k) and k not in codes)

    print(f"\n=== {path.name} ===")
    print(f"watched_stocks: 保留 {len(kept)} / 剔除 {len(dropped)}（原 {len(kept) + len(dropped)}）")
    for key, (weight, reason) in sorted(dropped.items(), key=lambda x: -x[1][0]):
        print(f"  ✗ {key:<12} {weight:.4f}  {reason}")
    print(f"recent_queries: 保留 {len(kept_recent)} / 剔除 {dropped_recent}（原 {len(recent)}）")
    if not_local:
        print(f"  ⚠ 本地库暂无行情（仅提示，不剔除）: {', '.join(not_local)}")

    if dry_run or (not dropped and not dropped_recent):
        return {"dropped": dropped, "dropped_recent": dropped_recent, "written": False}

    shutil.copy2(path, path.with_suffix(f".bak-{int(time.time())}.json"))
    data["watched_stocks"] = kept
    data["recent_queries"] = kept_recent
    data.setdefault("repaired_from", []).append({
        "at": time.time(),
        "kind": "watched_stocks_pollution_20261001",
        "removed_stocks": {k: round(v, 6) for k, (v, _) in dropped.items()},
        "removed_stocks_reason": {k: r for k, (_, r) in dropped.items()},
        "removed_recent_queries": dropped_recent,
        "note": "画像抽取器把市场报告文本切碎写入 watched_stocks（000001=上证指数码被当个股）；"
                "代码侧已于 2026-10-01 修复，此脚本只清存量。",
    })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"dropped": dropped, "dropped_recent": dropped_recent, "written": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="实际写入（默认仅预演）——用户数据，默认不落盘")
    ap.add_argument("--drop-codes", default=",".join(_DEFAULT_DROP_CODES),
                    help="按污染码剔除（逗号分隔），默认 000001")
    ap.add_argument("--keep", default="", help="额外白名单键（逗号分隔）")
    args = ap.parse_args()

    drop_codes = {c.strip() for c in args.drop_codes.split(",") if c.strip()}
    keep = {c.strip() for c in args.keep.split(",") if c.strip()}

    d = Path(get_user_profile_dir())
    if not d.is_dir():
        print(f"画像目录不存在: {d}")
        return 0
    total_stocks = total_recent = 0
    for f in sorted(d.glob("*.json")):
        if ".bak-" in f.name:            # 跳过备份，避免二次处理
            continue
        r = clean_file(f, dry_run=not args.apply, drop_codes=drop_codes, keep=keep)
        total_stocks += len(r.get("dropped") or {})
        total_recent += r.get("dropped_recent") or 0
    tag = "" if args.apply else "（预演，未写入；加 --apply 落盘）"
    print(f"\n{tag}合计：剔除 {total_stocks} 个污染标的键、{total_recent} 条测试/失败查询记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
