"""
文档切分 — 将长报告按语义边界切分为多个 chunk，每段独立可检索。

切分策略优先级: markdown 标题 > 双空行 > 单空行 > 硬截断
"""
import json
import re

_CHUNK_MIN_CHARS = 80     # 低于此长度不独立成段，合并到前一段
_CHUNK_MAX_CHARS = 1000   # 超过此长度强制切分（BGE-M3 最佳窗口 ~512 tokens）

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


def chunk_report(text: str) -> list[str]:
    """将长报告按段落/标题切分为多个 chunk，每个 chunk 独立可检索。"""
    if not text or len(text) <= _CHUNK_MAX_CHARS:
        return [text] if text else []

    # Step 1: 按 markdown 标题切分（## / ###）
    heading_pattern = re.compile(r'^#{2,4}\s+', re.MULTILINE)
    heading_positions = [m.start() for m in heading_pattern.finditer(text)]

    if len(heading_positions) >= 2:
        chunks = []
        for i, pos in enumerate(heading_positions):
            end = heading_positions[i + 1] if i + 1 < len(heading_positions) else len(text)
            chunk = text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
        if chunks:
            return _split_oversized(chunks)

    # Step 2: 按双空行切分（段落边界）
    paragraphs = re.split(r'\n\n\n+', text)
    if len(paragraphs) >= 2:
        chunks = [p.strip() for p in paragraphs if len(p.strip()) >= _CHUNK_MIN_CHARS]
        if chunks:
            return _split_oversized(chunks)

    # Step 3: 按单空行切分
    lines = text.split('\n')
    chunks = []
    current = ""
    for line in lines:
        if not line.strip() and len(current) >= _CHUNK_MIN_CHARS:
            chunks.append(current.strip())
            current = ""
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.strip())

    return _split_oversized(chunks) if chunks else [text]


def _split_oversized(chunks: list[str]) -> list[str]:
    """超过 _CHUNK_MAX_CHARS 的 chunk：优先按空行切分，仍超长则循环截断（不丢数据）。"""
    result = []
    for c in chunks:
        if len(c) <= _CHUNK_MAX_CHARS:
            result.append(c)
            continue

        sub = re.split(r'\n\n+', c)
        for s in sub:
            s = s.strip()
            if not s:
                continue
            while len(s) > _CHUNK_MAX_CHARS:
                cut = s[:_CHUNK_MAX_CHARS]
                last_period = max(cut.rfind('。'), cut.rfind('；'), cut.rfind('\n'))
                if last_period > _CHUNK_MIN_CHARS:
                    cut = cut[:last_period + 1]
                if len(cut) >= _CHUNK_MIN_CHARS:
                    result.append(cut)
                s = s[len(cut):].strip()
            if len(s) >= _CHUNK_MIN_CHARS:
                result.append(s)
    return result
