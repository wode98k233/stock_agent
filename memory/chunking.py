"""
文档切分 — 将长报告按语义边界切分为多个 chunk，每段独立自包含、可检索。

切分策略优先级: markdown 章节（子块携带章节标题）> 表格行分组（自带表头）> 空行 > 句边界
"""
import json
import re

_CHUNK_MIN_CHARS = 80     # 句边界硬切的最小保留长度
_CHUNK_MAX_CHARS = 1000   # 超过此长度强制切分（BGE-M3 最佳窗口 ~512 tokens）
_CHUNK_MIN_MERGE_CHARS = 120  # 小于此长度的切片并入相邻块（避免碎渣拖累 embedding 质量）

# dashboard 标记：报告尾部会追加 @@DASHBOARD_START@@{json}@@DASHBOARD_END@@，
# 前端用原始 JSON 渲染仪表盘卡片。但入库/向量化时原始 JSON 串是噪音——
# 既占 token 又污染 embedding（JSON 键名/引号/缩进无检索价值）。
# 因此归档前用 dashboard_to_text() 把 JSON 提炼成可读文本。
_DASHBOARD_RE = re.compile(
    r"@@DASHBOARD_START@@(.*?)(?:@@DASHBOARD_END@@|\Z)",
    re.DOTALL,
)
# 孤立 END 碎片：chunk 切断后可能只剩 JSON 尾巴（...}@@DASHBOARD_END@@），
# 无 START 标记、内容无检索价值，应整段移除。JSON 以独立行追加在报告尾部，
# 故按「行首 → END 标记」匹配移除整行。注意：必须在完整块（含 START）处理
# 之后执行——完整块被替换后其 END 标记已消失，此正则只命中真正的孤立碎片。
_DANGLING_END_RE = re.compile(r"[^\n]*?@@DASHBOARD_END@@")

# 前端图表占位符：@@CHART:uuid@@。纯前端渲染标记，无任何检索价值，
# 与 dashboard JSON 同理必须在入库前移除，否则记忆里会出现整段无正文的 UUID 串。
_CHART_RE = re.compile(r"@@CHART:[0-9a-fA-F-]{8,64}@@")


def strip_chart_markers(text: str) -> str:
    """移除报告中的 @@CHART:uuid@@ 图表占位符。"""
    if not text or "@@CHART" not in text:
        return text or ""
    return _CHART_RE.sub("", text).strip()


def _render_dashboard_text(data: dict) -> str:
    """把 dashboard JSON 提炼成可读文本（只保留有检索/记忆价值的字段）。

    前端渲染卡片用的是原始 JSON（消息原文里保留），记忆库只存提炼文本，
    保留核心结论/决策/要点等语义，去掉 JSON 语法噪音。
    """
    parts = []
    verdict = data.get("core_verdict")
    if isinstance(verdict, str) and verdict.strip():
        parts.append(f"核心结论：{verdict.strip()}")
    decision = data.get("decision_type")
    decision_map = {"buy": "买入", "hold": "持有/观望", "sell": "卖出"}
    if isinstance(decision, str) and decision in decision_map:
        parts.append(f"操作建议：{decision_map[decision]}")
    key_points = data.get("key_points")
    if isinstance(key_points, list):
        kps = [str(p) for p in key_points if str(p).strip()][:5]
        if kps:
            parts.append("要点：" + "；".join(kps))
    next_watch = data.get("next_watch")
    if isinstance(next_watch, list):
        nws = [str(p) for p in next_watch if str(p).strip()][:5]
        if nws:
            parts.append("关注：" + "；".join(nws))
    return "\n".join(parts).strip()


def dashboard_to_text(text: str) -> str:
    """把报告中的 dashboard JSON 块替换为可读文本。

    - JSON 完整可解析 → 提炼 core_verdict/decision/key_points 等为自然语言；
    - JSON 不完整（被截断的碎片）→ 尝试解析，失败则整块移除（碎片无价值）；
    - 无 dashboard 标记 → 原样返回。
    """
    if not text or "@@DASHBOARD" not in text:
        return text or ""

    def _repl(match):
        raw = match.group(1).strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                rendered = _render_dashboard_text(data)
                if rendered:
                    return rendered
        except (json.JSONDecodeError, TypeError):
            pass
        return ""  # 解析失败 → 移除碎片

    # 第一步：先处理完整块（含 START 的），提炼 JSON 为可读文本
    text = _DASHBOARD_RE.sub(_repl, text)
    # 第二步：移除孤立 END 碎片（此时完整块的 END 已被替换消失，剩余均为孤立碎片）
    text = _DANGLING_END_RE.sub("", text)
    return text.strip()


def strip_report_decor(text: str) -> str:
    """去掉报告中所有装饰性字符行（═══ / ─── / ▎ 等），只保留有实际内容的文本。"""
    lines = text.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        # 纯装饰线：全部由 ═ ─ 组成
        if re.match(r"^[═─]{4,}$", stripped):
            continue
        # 装饰夹标题：如 "══════ 市场日报 ══════" 或 "─── ▎ 核心结论 ───"
        # 提取标题文字，丢掉装饰字符
        cleaned = re.sub(r"[═─]{3,}", "", stripped).strip()
        cleaned = re.sub(r"▎\s*", "", cleaned).strip()
        if cleaned:
            out.append(cleaned)
        # 如果 cleaned 为空（整行被清掉），跳过
    return "\n".join(out).strip()


# ═══════════════════════════════════════════════════════════════
# 结构感知切分
# ═══════════════════════════════════════════════════════════════

def _is_table_line(line: str) -> bool:
    """判断一行是否属于 markdown 表格（以 | 开头并以 | 结尾）。"""
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _table_header_len(lines: list) -> int:
    """表格表头行数：列名行 + 分隔行（|---|---|）为 2，仅列名行为 1。"""
    if len(lines) >= 2 and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[1]):
        return 2
    return 1


def _hard_split(text: str) -> list:
    """兜底切分：按句/行/逗号边界循环切，保证单片不超长且不丢数据。"""
    pieces = []
    s = text
    while len(s) > _CHUNK_MAX_CHARS:
        cut = s[:_CHUNK_MAX_CHARS]
        pos = max(cut.rfind('。'), cut.rfind('；'), cut.rfind('！'), cut.rfind('？'),
                  cut.rfind('\n'), cut.rfind('，'), cut.rfind(' '))
        if pos > _CHUNK_MIN_CHARS:
            cut = cut[:pos + 1]
        if cut.strip():
            pieces.append(cut.strip())
        s = s[len(cut):].strip()
    if s.strip():
        pieces.append(s)
    return pieces


def _split_table_block(lines: list, max_chars: int) -> list:
    """超长表格按行分组；每组自带表头（列名+分隔行），保证各切片独立可读。

    修复：此前表格落入通用硬截断，在表格行中间切断导致"信息写一半"。
    """
    if not lines:
        return []
    header_n = _table_header_len(lines)
    header = "\n".join(lines[:header_n])
    budget = max_chars - len(header) - 1

    body = []
    for row in lines[header_n:]:
        # 单行超预算的极端情况：对该行做句边界硬切（罕见，接受残缺）
        if len(row) > budget > 0:
            body.extend(_hard_split(row))
        else:
            body.append(row)

    chunks, cur, cur_len = [], [], 0
    for row in body:
        if cur and cur_len + len(row) + 1 > budget:
            chunks.append(header + "\n" + "\n".join(cur))
            cur, cur_len = [], 0
        cur.append(row)
        cur_len += len(row) + 1
    if cur:
        chunks.append(header + "\n" + "\n".join(cur))
    return chunks


def _split_plain_text(text: str) -> list:
    """普通文本：整体不超长直接返回；否则按空行分段，段内超长再句边界硬切。"""
    text = text.strip("\n")
    if not text.strip():
        return []
    if len(text) <= _CHUNK_MAX_CHARS:
        return [text]
    result = []
    for para in re.split(r'\n\n+', text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= _CHUNK_MAX_CHARS:
            result.append(para)
        else:
            result.extend(_hard_split(para))
    return result


def _split_long_block(block: str) -> list:
    """把超长文本块切分为不超长的片段：表格行聚块分组，普通文本按空行/句边界。

    不丢弃任何内容（旧版会丢弃 <80 字的短段落）。
    """
    pieces = []
    table_buf = []
    text_buf = []

    def flush_table():
        nonlocal table_buf
        if table_buf:
            pieces.extend(_split_table_block(table_buf, _CHUNK_MAX_CHARS))
            table_buf = []

    def flush_text():
        nonlocal text_buf
        if text_buf:
            pieces.extend(_split_plain_text("\n".join(text_buf)))
            text_buf = []

    for line in block.split("\n"):
        if _is_table_line(line):
            flush_text()
            table_buf.append(line)
        else:
            flush_table()
            text_buf.append(line)
    flush_table()
    flush_text()
    return [p for p in pieces if p.strip()]


_SECTION_TITLE_RE = re.compile(r'^(#{2,4}\s+[^\n]+\n?)([\s\S]*)$')


def _split_section(chunk: str) -> list:
    """切分超长章节：子片段统一携带章节标题，单独看也知道在讲什么。"""
    m = _SECTION_TITLE_RE.match(chunk)
    if not m:
        return _split_long_block(chunk)
    title = m.group(1).strip()
    pieces = _split_long_block(m.group(2))
    return [f"{title}\n{p}" for p in pieces if p.strip()]


# ── R5（2026-09-23）：最小长度合并 + 表格语义摘要 ──────────────

_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def _merge_small_chunks(chunks: list,
                        min_chars: int = _CHUNK_MIN_MERGE_CHARS,
                        max_chars: int = _CHUNK_MAX_CHARS) -> list:
    """把过短切片并入相邻块，减少「碎渣」（<min_chars 的孤立片段 embedding 语义稀薄）。

    优先并入**前一块**（保持文档顺序）；首块过短且前块不存在时并入其后块。
    合并只在不超过 max_chars 时进行。
    """
    if len(chunks) <= 1:
        return list(chunks)
    merged: list = []
    for ch in chunks:
        if (merged and len(ch) < min_chars
                and len(merged[-1]) + 1 + len(ch) <= max_chars):
            merged[-1] = merged[-1] + "\n" + ch
        else:
            merged.append(ch)
    # 首块仍偏小 → 并入其后块
    if (len(merged) >= 2 and len(merged[0]) < min_chars
            and len(merged[0]) + 1 + len(merged[1]) <= max_chars):
        merged[1] = merged[0] + "\n" + merged[1]
        merged.pop(0)
    return merged


def _table_summary(chunk: str) -> str:
    """表格块的语义摘要（列名 + 首列行项），供 embedding 提升语义密度。

    非表格块（表格行占比 < 50% 或行数 < 3）返回空串。
    """
    lines = [ln for ln in chunk.split("\n") if ln.strip()]
    if not lines:
        return ""
    table_lines = [ln for ln in lines if _is_table_line(ln)]
    if len(table_lines) < 3 or len(table_lines) < len(lines) * 0.5:
        return ""
    cols = [c.strip() for c in table_lines[0].strip("|").split("|") if c.strip()]
    row_labels = []
    for ln in table_lines[1:]:
        if _TABLE_SEP_RE.match(ln):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if cells and cells[0] and cells[0] not in row_labels:
            row_labels.append(cells[0])
        if len(row_labels) >= 6:
            break
    if not cols and not row_labels:
        return ""
    parts = []
    if cols:
        parts.append("列：" + "、".join(cols[:6]))
    if row_labels:
        parts.append("行：" + "、".join(row_labels[:6]))
    return "【表格】" + "；".join(parts)


def _augment_table_chunks(chunks: list) -> list:
    """给表格块补一行语义摘要：有章节标题时插到标题之后（不破坏「以标题开头」契约）。"""
    out = []
    for ch in chunks:
        summary = _table_summary(ch)
        if not summary:
            out.append(ch)
            continue
        head, sep, rest = ch.partition("\n")
        if sep and head.lstrip().startswith("#"):
            out.append(f"{head}\n{summary}\n{rest}")
        else:
            out.append(f"{summary}\n{ch}")
    return out


def _finalize_chunks(chunks: list) -> list:
    """后处理：先合并碎渣，再给表格块补语义摘要。"""
    return _augment_table_chunks(_merge_small_chunks(chunks))


def chunk_report(text: str) -> list:
    """将长报告按章节/表格/段落切分为多个自包含 chunk，每个 chunk 独立可检索。"""
    if not text:
        return []
    if len(text) <= _CHUNK_MAX_CHARS:
        return _finalize_chunks([text])

    # Step 1: 按 markdown 标题切分章节（单标题的超长正文同样按章节处理）
    heading_pattern = re.compile(r'^#{2,4}\s+', re.MULTILINE)
    heading_positions = [m.start() for m in heading_pattern.finditer(text)]

    if heading_positions:
        chunks = []
        # 导言区（第一个标题之前：报告标题/数据截止/风险前置）不再丢弃
        prelude = text[:heading_positions[0]].strip()
        if prelude:
            if len(prelude) <= _CHUNK_MAX_CHARS:
                chunks.append(prelude)
            else:
                chunks.extend(_split_long_block(prelude))
        for i, pos in enumerate(heading_positions):
            end = heading_positions[i + 1] if i + 1 < len(heading_positions) else len(text)
            chunk = text[pos:end].strip()
            if not chunk:
                continue
            if len(chunk) <= _CHUNK_MAX_CHARS:
                chunks.append(chunk)
            else:
                chunks.extend(_split_section(chunk))
        return _finalize_chunks(chunks)

    # Step 2: 无章节结构 → 表格分组 + 空行/句边界切分
    return _finalize_chunks(_split_long_block(text))
