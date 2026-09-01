"""
CLI 启动横幅 & 退出画面
ANSI 彩色 + Unicode 框线渲染
"""
import json
import os
import random
import unicodedata

from utils.app_paths import get_questions_pool_path


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def _pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    WHITE = "\033[37m"
    BOLD_CYAN = "\033[1;36m"
    BOLD_YELLOW = "\033[1;33m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_WHITE = "\033[1;37m"

    @staticmethod
    def supports_color() -> bool:
        if os.getenv("NO_COLOR"):
            return False
        if os.getenv("TERM") == "dumb":
            return False
        # 打包态：仅在 VT100 启用成功时才输出 ANSI 颜色
        if getattr(__import__("sys"), "frozen", False) and not os.getenv("_VT100_ENABLED"):
            return False
        return hasattr(os, "isatty") and os.isatty(1)


_ansi = _Ansi()
_USE_COLOR = _ansi.supports_color()


def _c(code: str, text: str) -> str:
    if _USE_COLOR:
        return f"{code}{text}{_ansi.RESET}"
    return text


def _load_questions() -> list[dict]:
    path = get_questions_pool_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        pool = json.load(f)
    return pool.get("questions", [])


def _pick_questions(pool: list[dict], count: int = 3) -> list[dict]:
    if len(pool) <= count:
        return pool[:]
    picked = []
    used_tags = set()
    shuffled = pool[:]
    random.shuffle(shuffled)
    for q in shuffled:
        if len(picked) >= count:
            break
        q_tags = set(q.get("tags", []))
        if q_tags & used_tags:
            continue
        picked.append(q)
        used_tags |= q_tags
    for q in shuffled:
        if len(picked) >= count:
            break
        if q not in picked:
            picked.append(q)
    return picked[:count]


def build_banner(
    *,
    mode: str = "react_stock",
    model: str = "",
    trace: str = "off",
    skill_count: int = 0,
    template_count: int = 0,
    cache: str = "disabled",
) -> str:
    pool = _load_questions()
    picked = _pick_questions(pool) if pool else []

    inner_w = 56
    sep = "─" * inner_w

    title = _c(_ansi.BOLD_CYAN, "Stock Radar CLI  选股雷达")
    info_line = (
        f"mode: {mode}    model: {model}    trace: {trace}"
    )
    stats_line = (
        f"skills: {skill_count} loaded    "
        f"templates: {template_count} active    "
        f"cache: {cache}"
    )

    lines = [
        sep,
        f"  {title}",
        f"  {_c(_ansi.CYAN, info_line)}",
        f"  {_c(_ansi.CYAN, stats_line)}",
        sep,
        "",
        "  直接输入问题开始分析，或输入 /help 查看命令。",
        "",
        "  常用命令：",
        "    /status                 查看运行状态和最近日志",
        "    /mode list              查看并切换 Agent 模式",
        "    /skills list            查看已发现 Skill",
        "    /templates list         查看报告模板",
        "    /trace recent           查看最近执行链",
    ]

    if picked:
        lines.append("")
        header = _c(_ansi.BOLD_YELLOW, "今日推荐:")
        lines.append(f"  {header}")
        for i, q in enumerate(picked, 1):
            num = _c(_ansi.BOLD_GREEN, f"  {i}.")
            text = _c(_ansi.WHITE, q["text"])
            lines.append(f"{num} {text}")

    return "\n".join(lines)


def build_exit_stats(session_stats) -> str:
    if session_stats is None:
        return _c(_ansi.BOLD_WHITE, "👋 再见!")

    elapsed = session_stats.start_time
    import time
    duration = time.time() - elapsed
    if duration < 60:
        duration_str = f"{duration:.0f} 秒"
    else:
        mins, secs = divmod(int(duration), 60)
        duration_str = f"{mins} 分 {secs} 秒"

    w = 52
    sep = _c(_ansi.CYAN, f"{'━' * w}")
    header = _c(_ansi.BOLD_CYAN, "📊 会话统计")

    cached = getattr(session_stats, 'total_cached_tokens', 0)
    cache_line = f"  缓存命中    {cached:,}" if cached else ""

    lines = [
        sep,
        header,
        f"  查询次数    {session_stats.queries}",
        f"  Token 消耗  {session_stats.total_tokens:,}",
    ]
    if cache_line:
        lines.append(cache_line)
    lines += [
        f"  LLM 调用   {session_stats.total_llm_calls} 次",
        f"  工具调用    {session_stats.total_tool_calls} 次",
        f"  会话时长    {duration_str}",
        sep,
        "",
        _c(_ansi.BOLD_WHITE, "👋 再见!"),
    ]
    return "\n".join(lines)
