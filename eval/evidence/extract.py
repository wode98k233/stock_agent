"""文本证据抽取：数字 / 股票代码 / 日期，以及比对用的归一化。

被 `grounding.verify_numbers`（数字有无出处）与 `review.validator`（quote 能否命中）
共用，所以归一化必须只有一份实现。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── 归一化（比对用）────────────────────────────────────────────────────

_MD_MARKS = re.compile(r"[*_`>#~]+")
_WS = re.compile(r"\s+")
# 归一化时统一掉的全角/变体标点
_PUNCT_MAP = {
    "，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")",
    "％": "%", "－": "-", "—": "-", "–": "-", "　": " ",
    "“": '"', "”": '"', "‘": "'", "’": "'",
}
_PUNCT_RE = re.compile("|".join(re.escape(k) for k in _PUNCT_MAP))


def normalize_text(text: str) -> str:
    """归一化文本用于 substring 比对：去 markdown 标记、统一标点、压平空白。

    注意：**不改数字本身**（不改千分位、不去单位），数字层面的等价由
    `numbers_match` 负责，否则会误伤「12.3%」与「12.3」这类本该区分的场景。
    """
    if not text:
        return ""
    s = str(text)
    s = _PUNCT_RE.sub(lambda m: _PUNCT_MAP[m.group()], s)
    s = _MD_MARKS.sub("", s)
    s = _WS.sub(" ", s)
    return s.strip()


def contains_quote(haystack: str, quote: str) -> bool:
    """quote 是否能在 haystack 中原文命中（两边都归一化后做 substring）。"""
    if not quote or not haystack:
        return False
    return normalize_text(quote) in normalize_text(haystack)


# ── 数字 ──────────────────────────────────────────────────────────────

# 乘数单位：参与数值换算
_MULTIPLIERS: dict[str, float] = {
    "万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2,
}
# 注记单位：保留在 raw/unit 里，但不换算数值
_ANNOTATIONS = ("%", "倍", "元", "点", "天", "次", "年", "月", "日", "只", "家", "个")
_UNIT_ALT = "|".join(sorted(list(_MULTIPLIERS) + list(_ANNOTATIONS), key=len, reverse=True))

_NUM_TOKEN = re.compile(
    r"(?<![\d.])"                                        # 左侧不能紧跟数字或小数点
    r"([-+−])?"                                          # 符号
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"      # 数值（含千分位）
    rf"\s*({_UNIT_ALT})?"                                # 可选单位
)

_STOCK_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CONTEXT_WINDOW = 24


@dataclass(frozen=True)
class NumberToken:
    """一个数字证据。`value` 已按乘数单位换算，`unit` 保留原文单位。"""

    raw: str
    value: float
    unit: str
    span: tuple[int, int]
    context: str

    @property
    def is_percent(self) -> bool:
        return self.unit == "%"

    def to_dict(self) -> dict:
        return {"raw": self.raw, "value": self.value, "unit": self.unit,
                "context": self.context}


def extract_numbers(text: str, *, skip_stock_codes: bool = True) -> list[NumberToken]:
    """抽出文本里的数字。

    `skip_stock_codes`：跳过 6 位纯数字（A 股代码）。默认开启——代码在 A 股语境下
    几乎总是标的本身、且通常由用户给出（已在来源白名单里），把它当「待核验数字」
    只会制造噪音。代码请用 `extract_stock_codes` 单独取。
    """
    if not text:
        return []
    s = str(text)
    out: list[NumberToken] = []
    for m in _NUM_TOKEN.finditer(s):
        sign, num, unit = m.group(1), m.group(2), m.group(3) or ""
        if skip_stock_codes and not unit and "." not in num and "," not in num \
                and len(num) == 6:
            continue
        try:
            base = float(num.replace(",", ""))
        except ValueError:
            continue
        if sign in ("-", "−"):
            base = -base
        value = base * _MULTIPLIERS.get(unit, 1.0)
        start, end = m.span()
        out.append(NumberToken(
            raw=m.group(0).strip(),
            value=value,
            unit=unit,
            span=(start, end),
            context=s[max(0, start - _CONTEXT_WINDOW):end + _CONTEXT_WINDOW].strip(),
        ))
    return out


def extract_stock_codes(text: str) -> list[str]:
    """抽出 6 位 A 股代码（去重、保序）。"""
    if not text:
        return []
    seen, out = set(), []
    for code in _STOCK_CODE.findall(str(text)):
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def numbers_match(a: NumberToken | float, b: NumberToken | float,
                  rel_tol: float = 0.01) -> bool:
    """两个数字是否可视为同一个值。

    容忍：千分位（已在 value 里去掉）、乘数单位换算（万/亿）、
    四舍五入（相对误差 ≤ rel_tol）、以及「0.123 ↔ 12.3%」这类比值/百分数混用。
    """
    va = a.value if isinstance(a, NumberToken) else float(a)
    vb = b.value if isinstance(b, NumberToken) else float(b)

    if _close(va, vb, rel_tol):
        return True
    # 比值 ↔ 百分数：一方带 % 时，另一方的 1/100 或 100 倍也算命中
    a_pct = isinstance(a, NumberToken) and a.is_percent
    b_pct = isinstance(b, NumberToken) and b.is_percent
    if a_pct != b_pct:
        if a_pct and (_close(va, vb * 100, rel_tol) or _close(va, vb / 100, rel_tol)):
            return True
        if b_pct and (_close(vb, va * 100, rel_tol) or _close(vb, va / 100, rel_tol)):
            return True
    return False


def _close(x: float, y: float, rel_tol: float) -> bool:
    if x == y:
        return True
    denom = max(abs(x), abs(y))
    if denom == 0:
        return False
    return abs(x - y) / denom <= rel_tol


# ── 日期 ──────────────────────────────────────────────────────────────

_DATE_PATTERNS = (
    re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),           # 2026-06-05
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),  # 2026年6月5日
    # 年月（后面不能再跟「日」，否则会与上面那条重叠匹配出 2026-06-01）
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d{1,2}\s*日)"),
)


def extract_dates(text: str) -> list[str]:
    """抽出日期并归一化为 `YYYY-MM-DD`（只有年月时补 `-01`）。去重保序。

    多条模式可能命中同一段文本（如「2026年6月5日」同时满足完整日期与年月），
    因此按 span 去重：重叠的匹配只保留先命中的那条。
    """
    if not text:
        return []
    s = str(text)
    hits: list[tuple[int, int, str]] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(s):
            y, mo = int(m.group(1)), int(m.group(2))
            d = int(m.group(3)) if (m.lastindex or 0) >= 3 else 1
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            hits.append((m.start(), m.end(), f"{y:04d}-{mo:02d}-{d:02d}"))

    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))   # 位置升序；同起点取更长的
    seen_spans: list[tuple[int, int]] = []
    seen_iso: set[str] = set()
    out: list[str] = []
    for start, end, iso in hits:
        if any(start < e and s0 < end for s0, e in seen_spans):
            continue   # 与已接受区间重叠
        seen_spans.append((start, end))
        if iso not in seen_iso:
            seen_iso.add(iso)
            out.append(iso)
    return out


def latest_date(text: str) -> str | None:
    """文本里最新的日期（ISO 字符串比较即可，格式已统一）。"""
    dates = extract_dates(text)
    return max(dates) if dates else None
