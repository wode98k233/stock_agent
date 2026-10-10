"""工具输出压缩语料库：导出、切分、回放与指标计算。

用途：
- 从 `agent_trace.db` 导出历史工具输出为 train/test 语料（`scripts/devtools/export_tool_corpus.py`）
- 离线回放规则压缩器并输出指标（`scripts/devtools/tool_compress_replay.py`）
- 单元测试用内存 SQLite 构造同构数据

设计约定：
- 语料按工具分文件：`{tool}.train.jsonl` / `{tool}.test.jsonl` / `{tool}.all.jsonl`
- 记录字段见 `build_records` 输出，核心为 raw_output / llm_compressed / tool_input / query
- 压缩器接口：`compressor(tool_name: str, tool_args: dict, content: str) -> str`
- 数字/代码召回率仅作辅助护栏，任务相关的关键信息保留用离线 LLM judge 评估
"""
import gzip
import hashlib
import json
import os
import random
import re
import time
from collections import defaultdict

CORPUS_VERSION = 1
DEFAULT_SMALL_LEN = 500

DEFAULT_SELECTION = {
    "compressed": 0,
    "large": 15,
    "small": 10,
    "error": 10,
    "empty": 3,
}

# 配额语义：<0 丢弃该类别；=0 全部保留；>0 上限

_TOOL_NAME_RE = re.compile(r"工具\s*[:：][^A-Za-z0-9_]{0,8}([A-Za-z_][A-Za-z0-9_]*)")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_STOCK_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_DATE_RE = re.compile(r"\d{4}[-./]?\d{1,2}[-./]?\d{1,2}")


def parse_tool_args(input_text: str) -> dict:
    """解析工具调用参数（steps.input），兼容 JSON 与 Python 字面量。"""
    if not input_text:
        return {}
    for loader in (json.loads, _literal_eval):
        try:
            data = loader(input_text)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _literal_eval(text: str):
    import ast

    return ast.literal_eval(text)


def extract_tool_name_from_compress_input(input_text: str) -> str | None:
    """从压缩 LLM 调用的 input 中提取工具名（消息形如 `工具: xxx\\n\\n输出内容`）。"""
    if not input_text:
        return None
    m = _TOOL_NAME_RE.search(input_text)
    return m.group(1) if m else None


def extract_llm_output(output_text: str) -> str:
    """解析 LLM step 的 output 为纯文本（trace 存的是消息 JSON 数组）。"""
    if not output_text:
        return ""
    try:
        data = json.loads(output_text)
    except Exception:
        return output_text.strip()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                return item["content"].strip()
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"].strip()
    return output_text.strip()


def _decode_strings(input_text: str) -> list[str]:
    try:
        data = json.loads(input_text)
    except Exception:
        return [input_text or ""]
    strings: list[str] = []

    def walk(node):
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(data)
    return strings


def _word_key(text: str, limit: int = 120) -> str:
    return re.sub(r"\W+", "", (text or "")[:limit])


def _verify_pair(candidate_raw: str, compress_input_text: str) -> bool:
    """用「非单词字符归一后的前缀包含」校验压缩输入与工具原始输出配对。"""
    key = _word_key(candidate_raw)
    if not key:
        return False
    return any(key in re.sub(r"\W+", "", s) for s in _decode_strings(compress_input_text))


def _looks_like_error(raw: str) -> bool:
    if not raw:
        return False
    if "执行失败" in raw:
        return True
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if isinstance(data, dict):
        if data.get("status") == "failed":
            return True
        if data.get("error"):
            return True
    return False


def _is_empty(raw: str) -> bool:
    if not raw:
        return True
    try:
        data = json.loads(raw)
    except Exception:
        return False
    return isinstance(data, (list, dict)) and len(data) == 0


def classify_record(record: dict, small_len: int = DEFAULT_SMALL_LEN) -> str:
    """记录分类：compressed / error / empty / small / large。"""
    raw = record.get("raw_output") or ""
    if record.get("llm_compressed"):
        return "compressed"
    if _is_empty(raw):
        return "empty"
    if _looks_like_error(raw):
        return "error"
    if len(raw) <= small_len:
        return "small"
    return "large"


def build_records(conn, verify_pairs: bool = True) -> tuple[list[dict], dict]:
    """从 trace 数据库读取工具输出并配对 LLM 压缩基线。

    Returns:
        (records, stats)；records 未去重，stats 记录配对情况。
    """
    tool_steps = []
    for step_id, run_id, tool, input_text, output_text, started_at in conn.execute(
        "select id, run_id, step_name, input, output, started_at from steps "
        "where step_type='tool' and output is not null and output != ''"
    ):
        if not tool or not output_text:
            continue
        args = parse_tool_args(input_text)
        query = ""
        if isinstance(args, dict):
            query = str(args.get("query") or args.get("question") or "")
        tool_steps.append(
            {
                "id": f"{tool}-{step_id}",
                "step_id": int(step_id),
                "run_id": run_id or "",
                "tool": tool,
                "tool_input": args,
                "query": query,
                "raw_output": output_text,
                "raw_len": len(output_text),
                "raw_md5": hashlib.md5(output_text.encode("utf-8")).hexdigest(),
                "started_at": started_at or "",
            }
        )

    tool_steps.sort(key=lambda r: (r["run_id"], r["started_at"], r["step_id"]))

    compress_steps = []
    for step_id, run_id, input_text, output_text, started_at in conn.execute(
        "select id, run_id, input, output, started_at from steps "
        "where step_type='llm' and extra like ?",
        ("%react-tool-compress%",),
    ):
        tool = extract_tool_name_from_compress_input(input_text or "")
        llm_output = extract_llm_output(output_text or "")
        if not tool or not llm_output:
            continue
        compress_steps.append(
            {
                "step_id": int(step_id),
                "run_id": run_id or "",
                "tool": tool,
                "input_text": input_text or "",
                "llm_output": llm_output,
                "started_at": started_at or "",
            }
        )

    compress_steps.sort(key=lambda r: (r["run_id"], r["started_at"], r["step_id"]))

    by_key = defaultdict(list)
    for record in tool_steps:
        by_key[(record["run_id"], record["tool"])].append(record)

    stats = {
        "tool_steps": len(tool_steps),
        "compress_steps": len(compress_steps),
        "verified": 0,
        "nearest": 0,
        "unmatched": 0,
        "duplicate": 0,
    }
    used: set[int] = set()
    for step in compress_steps:
        candidates = by_key.get((step["run_id"], step["tool"]), [])
        if not candidates:
            stats["unmatched"] += 1
            continue
        pool = [
            r
            for r in candidates
            if not step["started_at"] or not r["started_at"] or r["started_at"] <= step["started_at"]
        ] or candidates
        chosen = None
        fallback = None
        for record in reversed(pool):
            if record["step_id"] in used:
                continue
            if fallback is None:
                fallback = record
            if not verify_pairs or _verify_pair(record["raw_output"], step["input_text"]):
                chosen = record
                break
        if chosen is None:
            chosen = fallback
            label = "nearest"
        else:
            label = "verified"
        if chosen is None or chosen["step_id"] in used:
            stats["unmatched" if chosen is None else "duplicate"] += 1
            continue
        chosen["llm_compressed"] = step["llm_output"]
        chosen["llm_len"] = len(step["llm_output"])
        chosen["pairing"] = label
        used.add(chosen["step_id"])
        stats[label] += 1

    return tool_steps, stats


def dedup_records(records: list[dict]) -> tuple[list[dict], int]:
    """按 (tool, raw_md5) 去重，优先保留带 LLM 基线的记录，保持首次出现顺序。"""
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    duplicates = 0
    for record in records:
        key = (record["tool"], record["raw_md5"])
        if key not in best:
            best[key] = record
            order.append(key)
            continue
        duplicates += 1
        current = best[key]
        if record.get("llm_compressed") and not current.get("llm_compressed"):
            best[key] = record
    return [best[key] for key in order], duplicates


def _pick(records: list[dict], quota: int, seed_key: str) -> list[dict]:
    if quota < 0 or not records:
        return []
    if quota == 0 or len(records) <= quota:
        return list(records)
    head_count = max(1, quota // 3)
    head = records[:head_count]
    rest = list(records[head_count:])
    random.Random(seed_key).shuffle(rest)
    return head + rest[: quota - head_count]


def select_records(
    records: list[dict],
    quotas: dict | None = None,
    small_len: int = DEFAULT_SMALL_LEN,
    seed: int = 42,
) -> list[dict]:
    """按工具与类别配额采样，保证大样本/极端尺寸覆盖。"""
    merged_quotas = {**DEFAULT_SELECTION, **(quotas or {})}
    per_tool: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        record["category"] = classify_record(record, small_len)
        per_tool[record["tool"]].append(record)

    selected: list[dict] = []
    for tool in sorted(per_tool):
        ordered = sorted(per_tool[tool], key=lambda r: (-r["raw_len"], r["id"]))
        by_category: dict[str, list[dict]] = defaultdict(list)
        for record in ordered:
            by_category[record["category"]].append(record)
        for category, quota in merged_quotas.items():
            selected.extend(_pick(by_category.get(category, []), quota, f"{seed}:{tool}:{category}"))
    return selected


def _split_key(record: dict) -> str:
    query = _DATE_RE.sub("#date#", (record.get("query") or "").strip().lower())
    if query:
        return f"{record['tool']}|{query}"
    return f"{record['tool']}|{record.get('raw_md5', '')}"


def split_records(
    records: list[dict],
    ratio: float = 0.8,
    mode: str = "hash",
    seed: int = 42,
) -> list[dict]:
    """切分 train/test：hash 模式按查询分组稳定切分，避免近重复泄漏。"""
    if mode == "none":
        for record in records:
            record["split"] = "all"
        return records
    if mode == "temporal":
        ordered = sorted(records, key=lambda r: (r.get("started_at") or "", r["id"]))
        cutoff = int(len(ordered) * ratio)
        train_ids = {r["id"] for r in ordered[:cutoff]}
        for record in records:
            record["split"] = "train" if record["id"] in train_ids else "test"
        return _balance_splits(records)
    threshold = round(ratio * 1000)
    for record in records:
        digest = hashlib.md5(f"{seed}:{_split_key(record)}".encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % 1000
        record["split"] = "train" if bucket < threshold else "test"
    return _balance_splits(records)


def _balance_splits(records: list[dict]) -> list[dict]:
    """每个工具尽量保证 train/test 均非空（样本数 >= 4 时）。

    多分组时整体移动一个查询分组，保持「同查询同切分」；单分组时只能移动单条。
    """
    per_tool: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        per_tool[record["tool"]].append(record)
    for tool, items in per_tool.items():
        if len(items) < 4:
            continue
        splits = {r["split"] for r in items}
        if len(splits) != 1:
            continue
        target = "test" if splits == {"train"} else "train"
        groups: dict[str, list[dict]] = defaultdict(list)
        for record in items:
            groups[_split_key(record)].append(record)
        if len(groups) >= 2:
            last_key = sorted(groups, key=lambda key: max(r["id"] for r in groups[key]))[-1]
            for record in groups[last_key]:
                record["split"] = target
        else:
            sorted(items, key=lambda r: r["id"])[-1]["split"] = target
    return records


def export_corpus(
    records: list[dict],
    out_dir: str,
    extra_manifest: dict | None = None,
    gzip_output: bool = True,
) -> dict:
    """按工具/切分落盘 JSONL（默认 gzip），并写 manifest.json。

    写入前清理目录下旧的语料文件（*.jsonl / *.jsonl.gz），避免新旧格式并存。
    """
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):
        if name.endswith((".jsonl", ".jsonl.gz")):
            os.remove(os.path.join(out_dir, name))

    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[record["tool"]][record["split"]].append(record)

    suffix = ".jsonl.gz" if gzip_output else ".jsonl"
    files: dict[str, int] = {}
    tool_stats: dict[str, dict] = {}
    for tool in sorted(grouped):
        tool_files = {}
        for split in ("train", "test", "all"):
            items = grouped[tool].get(split)
            if not items:
                continue
            path = os.path.join(out_dir, f"{tool}.{split}{suffix}")
            opener = gzip.open if gzip_output else open
            with opener(path, "wt", encoding="utf-8") as handle:
                for record in sorted(items, key=lambda r: r["id"]):
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            name = f"{tool}.{split}"
            files[name] = len(items)
            tool_files[split] = len(items)
        categories: dict[str, int] = defaultdict(int)
        raw_total = 0
        for split, items in grouped[tool].items():
            for record in items:
                categories[record["category"]] += 1
                raw_total += record["raw_len"]
        tool_stats[tool] = {
            "files": tool_files,
            "categories": dict(sorted(categories.items())),
            "raw_total_chars": raw_total,
        }

    manifest = {
        "corpus_version": CORPUS_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "format": "jsonl.gz" if gzip_output else "jsonl",
        "records": len(records),
        "files": files,
        "tools": tool_stats,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def load_corpus(
    corpus_dir: str,
    tools: set[str] | None = None,
    splits: tuple[str, ...] = ("train", "test", "all"),
) -> list[dict]:
    """读取语料目录下的 JSONL（可按工具/切分过滤）。"""
    records: list[dict] = []
    if not os.path.isdir(corpus_dir):
        return records
    for name in sorted(os.listdir(corpus_dir)):
        if name.endswith(".jsonl.gz"):
            stem = name[: -len(".jsonl.gz")]
            opener = lambda path: gzip.open(path, "rt", encoding="utf-8")
        elif name.endswith(".jsonl"):
            stem = name[: -len(".jsonl")]
            opener = lambda path: open(path, encoding="utf-8")
        else:
            continue
        if "." not in stem:
            continue
        tool, split = stem.rsplit(".", 1)
        if tools and tool not in tools:
            continue
        if splits and split not in splits:
            continue
        with opener(os.path.join(corpus_dir, name)) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                record.setdefault("split", split)
                records.append(record)
    return records


def extract_numbers(text: str) -> set[str]:
    """提取数字 token（去千分位），用于辅助护栏指标。"""
    return {m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")}


def extract_stock_codes(text: str) -> set[str]:
    """提取 6 位股票代码。"""
    return set(_STOCK_CODE_RE.findall(text or ""))


def extract_leaf_values(text: str, max_len: int = 500) -> set[str]:
    """提取 JSON 所有叶子标量值（排除空值/零值/超长文本块），用于值保留率审计。"""
    try:
        data = json.loads(text)
    except Exception:
        return set()
    values: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif node is None or isinstance(node, bool):
            return
        else:
            text_value = str(node)
            if not text_value.strip() or len(text_value) > max_len:
                return
            if isinstance(node, (int, float)) and node == 0:
                return
            values.add(text_value)

    walk(data)
    return values


def value_recall(reference: set[str], output: str) -> float | None:
    """参考值集合在输出文本中的保留比例。"""
    if not reference:
        return None
    kept = sum(1 for value in reference if value in (output or ""))
    return kept / len(reference)


def token_recall(reference: set[str], candidate: set[str]) -> float | None:
    """参考集合被保留的比例；参考为空返回 None。"""
    if not reference:
        return None
    return len(reference & candidate) / len(reference)


def _mean(values: list[float | None]) -> float | None:
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def compute_metrics(
    records: list[dict],
    compressor=None,
    threshold: int = 3000,
) -> dict:
    """回放压缩器并汇总指标。

    compressor: 可调用对象 comp(tool_name, tool_args, content) -> str；None 时仅统计 LLM 基线。
    """
    per_tool: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        raw = record.get("raw_output") or ""
        row = {
            "id": record.get("id"),
            "tool": record.get("tool"),
            "split": record.get("split"),
            "category": record.get("category"),
            "scrubbed": record.get("scrubbed", False),
            "query": record.get("query") or "",
            "raw_len": len(raw),
            "raw_output": raw,
            "llm_compressed": record.get("llm_compressed"),
            "llm_len": record.get("llm_len"),
            "rule_output": None,
            "rule_len": None,
            "skip_llm": None,
            "error": None,
        }
        if compressor is not None:
            try:
                output = compressor(record.get("tool") or "", record.get("tool_input") or {}, raw)
            except Exception as exc:
                output = raw
                row["error"] = f"{type(exc).__name__}: {exc}"
            if not isinstance(output, str):
                output = str(output)
            row["rule_output"] = output
            row["rule_len"] = len(output)
            row["skip_llm"] = 0 < len(output) <= threshold
            row["rule_num_recall"] = token_recall(extract_numbers(raw), extract_numbers(output))
            row["rule_code_recall"] = token_recall(extract_stock_codes(raw), extract_stock_codes(output))
            row["rule_value_recall"] = value_recall(extract_leaf_values(raw), output)
        if record.get("llm_compressed"):
            llm = record["llm_compressed"]
            row["llm_num_recall"] = token_recall(extract_numbers(raw), extract_numbers(llm))
            row["llm_code_recall"] = token_recall(extract_stock_codes(raw), extract_stock_codes(llm))
        per_tool[record.get("tool") or "?"].append(row)

    summary = {}
    for tool, rows in sorted(per_tool.items()):
        with_llm = [r for r in rows if r.get("llm_len")]
        rule_rows = [r for r in rows if r.get("rule_len") is not None]
        summary[tool] = {
            "n": len(rows),
            "raw_avg": round(_mean([r["raw_len"] for r in rows]) or 0),
            "with_llm": len(with_llm),
            "llm_ratio": _ratio(_mean([r["llm_len"] for r in with_llm]), _mean([r["raw_len"] for r in with_llm])),
            "llm_num_recall": _mean([r.get("llm_num_recall") for r in with_llm]),
            "rule_n": len(rule_rows),
            "rule_ratio": _ratio(_mean([r["rule_len"] for r in rule_rows]), _mean([r["raw_len"] for r in rule_rows])),
            "rule_skip_rate": (sum(1 for r in rule_rows if r["skip_llm"]) / len(rule_rows)) if rule_rows else None,
            "rule_num_recall": _mean([r.get("rule_num_recall") for r in rule_rows]),
            "rule_code_recall": _mean([r.get("rule_code_recall") for r in rule_rows]),
            "rule_value_recall": _mean([r.get("rule_value_recall") for r in rule_rows]),
            "errors": sum(1 for r in rows if r.get("error")),
        }
    return {"summary": summary, "rows": [r for rows in per_tool.values() for r in rows]}


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if not numerator or not denominator:
        return None
    return numerator / denominator


COMPARISON_COLUMNS = [
    ("id", "记录ID"),
    ("tool", "工具"),
    ("split", "切分"),
    ("category", "类别"),
    ("scrubbed", "已脱敏(LLM基线不可比)"),
    ("query", "查询"),
    ("raw_len", "原始长度"),
    ("rule_len", "规则长度"),
    ("llm_len", "LLM长度"),
    ("rule_changed", "规则有变化"),
    ("skip_llm", "规则可跳过LLM"),
    ("raw_output", "原始工具调用结果"),
    ("rule_output", "规则压缩结果"),
    ("llm_compressed", "LLM压缩结果"),
]


def write_comparison_csv(rows: list[dict], path: str) -> None:
    """导出「原始 / 规则 / LLM」全量内容 CSV（utf-8-sig，Excel 可直接打开）。"""
    import csv

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in COMPARISON_COLUMNS])
        for row in rows:
            raw = row.get("raw_output")
            rule = row.get("rule_output")
            computed = dict(row)
            computed["rule_changed"] = bool(rule is not None and rule != raw)
            writer.writerow([
                "" if computed.get(key) is None else computed.get(key)
                for key, _ in COMPARISON_COLUMNS
            ])


# ── 敏感数据脱敏（持仓/账户/模拟盘）──
SENSITIVE_TOOLS = {
    "get_positions",
    "get_position_detail",
    "get_pnl_summary",
    "get_trade_history",
    "mx_moni_operation",
    "mx_zixuan_manage",
}
_NAME_KEYS = {"stock_name", "name", "secName", "sec_name", "证券名称", "股票名称"}
_CODE_KEYS = {"stock_code", "code", "secCode", "sec_code", "股票代码"}


def _shift_digits(text: str) -> str:
    """逐位数字偏移（+3 mod 10），保留长度与格式但使数值失效。"""
    return re.sub(r"\d", lambda m: str((int(m.group(0)) + 3) % 10), text or "")


def _scrub_factor(key: str) -> float:
    return random.Random(f"scrub:{key}").uniform(0.6, 1.4)


def _collect_names(node, out: set) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _NAME_KEYS and isinstance(value, str) and value:
                out.add(value)
            else:
                _collect_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_names(value, out)


def _scrub_node(node, seed_key: str, mapping: dict):
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key in _NAME_KEYS and isinstance(value, str):
                result[key] = mapping.get(value, "示例标的")
            elif key in _CODE_KEYS and isinstance(value, str):
                result[key] = _shift_digits(value)
            elif key == "query" and isinstance(value, str):
                result[key] = "【已脱敏查询】"
            else:
                result[key] = _scrub_node(value, f"{seed_key}.{key}", mapping)
        return result
    if isinstance(node, list):
        return [_scrub_node(value, seed_key, mapping) for value in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node * _scrub_factor(seed_key))
    if isinstance(node, float):
        return round(node * _scrub_factor(seed_key), 4)
    if isinstance(node, str):
        return _shift_digits(node)
    return node


def scrub_record(record: dict) -> dict:
    """对敏感工具记录做脱敏：名称替换、代码/日期数字偏移、数值确定性缩放，保留结构。"""
    if (record.get("tool") or "") not in SENSITIVE_TOOLS:
        return record
    try:
        data = json.loads(record.get("raw_output") or "")
    except Exception:
        return record

    names: set = set()
    _collect_names(data, names)
    mapping = {name: f"示例标的{chr(65 + i % 26)}" for i, name in enumerate(sorted(names))}

    def _replace_names(text: str) -> str:
        for real, fake in mapping.items():
            text = text.replace(real, fake)
        return text

    scrubbed = dict(record)
    raw_json = json.dumps(_scrub_node(data, record.get("tool") or "", mapping), ensure_ascii=False)
    scrubbed["raw_output"] = _replace_names(raw_json)
    scrubbed["raw_len"] = len(scrubbed["raw_output"])
    scrubbed["raw_md5"] = hashlib.md5(scrubbed["raw_output"].encode("utf-8")).hexdigest()

    llm = record.get("llm_compressed")
    if llm:
        scrubbed["llm_compressed"] = _shift_digits(_replace_names(llm))
        scrubbed["llm_len"] = len(scrubbed["llm_compressed"])

    if scrubbed.get("query"):
        scrubbed["query"] = "【已脱敏查询】"
    if isinstance(scrubbed.get("tool_input"), dict):
        scrubbed["tool_input"] = {
            key: ("【已脱敏查询】" if key == "query" and isinstance(value, str) else
                  (_shift_digits(_replace_names(value)) if isinstance(value, str) else value))
            for key, value in scrubbed["tool_input"].items()
        }
    scrubbed["scrubbed"] = True
    return scrubbed


JUDGE_PROMPT = """你是工具输出压缩质量评审员。

【工具】{tool}
【用户查询】{query}
【原始输出】
{raw}

【压缩后输出】
{compressed}

请判断：压缩后输出是否保留了「回答该查询所需的关键信息」？
注意：
- 不要要求保留所有数字，只关注回答查询所需的关键事实、实体、数值、结论和趋势。
- 若压缩后输出丢失了回答查询所必需的信息，判为不通过。
- 若压缩后输出为空、截断严重或信息无法回答查询，也判为不通过。
只输出 JSON（不要代码块）：{{"retained": true/false, "missing": ["..."], "comment": "一句话"}}
"""


def build_judge_prompt(record: dict, compressed: str, raw_limit: int = 6000) -> str:
    raw = record.get("raw_output") or ""
    return JUDGE_PROMPT.format(
        tool=record.get("tool") or "?",
        query=record.get("query") or "（未提供）",
        raw=raw[:raw_limit],
        compressed=(compressed or "")[:4000],
    )


def parse_judge_result(content: str) -> dict:
    """解析 judge 返回的 JSON（容忍代码块与前后缀文本）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {
                "retained": bool(data.get("retained")),
                "missing": data.get("missing") or [],
                "comment": str(data.get("comment") or ""),
            }
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return {
                    "retained": bool(data.get("retained")),
                    "missing": data.get("missing") or [],
                    "comment": str(data.get("comment") or ""),
                }
        except Exception:
            pass
    return {"retained": None, "missing": [], "comment": f"解析失败: {text[:120]}"}
