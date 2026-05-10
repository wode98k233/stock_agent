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


def build_banner() -> str:
    pool = _load_questions()
    picked = _pick_questions(pool) if pool else []

    inner_w = 54
    top = _c(_ansi.BOLD_CYAN, f"╔{'═' * inner_w}╗")
    bot = _c(_ansi.BOLD_CYAN, f"╚{'═' * inner_w}╝")

    title_raw = "📡 选股雷达 Stock Radar"
    subtitle_raw = "智能选股 Agent (ReAct / Plan&Solve / Unified)"
    title = _c(_ansi.BOLD_CYAN, _pad_right(title_raw, inner_w - 2))
    subtitle = _c(_ansi.CYAN, _pad_right(subtitle_raw, inner_w - 2))

    lines = [
        top,
        f"║  {title}║",
        f"║  {subtitle}║",
        bot,
    ]

    if picked:
        lines.append("")
        header = _c(_ansi.BOLD_YELLOW, "💡 今日推荐:")
        lines.append(header)
        for i, q in enumerate(picked, 1):
            num = _c(_ansi.BOLD_GREEN, f"  {i}.")
            text = _c(_ansi.WHITE, q["text"])
            lines.append(f"{num} {text}")

    lines.append("")
    help_hint = _c(_ansi.DIM, "输入 help 查看命令 | exit 退出")
    lines.append(help_hint)

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

    lines = [
        sep,
        header,
        f"  查询次数    {session_stats.queries}",
        f"  Token 消耗  {session_stats.total_tokens:,}",
        f"  LLM 调用   {session_stats.total_llm_calls} 次",
        f"  工具调用    {session_stats.total_tool_calls} 次",
        f"  会话时长    {duration_str}",
        sep,
        "",
        _c(_ansi.BOLD_WHITE, "👋 再见!"),
    ]
    return "\n".join(lines)
