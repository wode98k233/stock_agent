from agents.analysis.dashboard_schema import DashboardData

W = 56

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_green": "\033[42m",
    "bg_yellow": "\033[43m",
    "bg_red": "\033[41m",
}


def _c(name: str, text: str) -> str:
    return f"{_ANSI.get(name, '')}{text}{_ANSI['reset']}"


def _line(char: str = "─") -> str:
    return char * W


def _center(text: str) -> str:
    return text.center(W)


_DECISION_META = {
    "buy": ("bg_green", " 🟢 BUY  ", "可参与", "green"),
    "hold": ("bg_yellow", " 🟡 HOLD ", "观望为主", "yellow"),
    "sell": ("bg_red", " 🔴 SELL ", "建议卖出", "red"),
}


def _sentiment_meta(score: int) -> tuple[str, str]:
    if score <= 30:
        return "偏恐惧", "red"
    if score <= 45:
        return "偏谨慎", "yellow"
    if score <= 55:
        return "中性", "yellow"
    if score <= 70:
        return "偏积极", "green"
    return "偏乐观", "green"


_STATUS_META = {
    "positive": ("✅", "green"),
    "warning": ("⚠️", "yellow"),
    "negative": ("❌", "red"),
}


_RISK_META = {
    "high": ("bg_red", " 高 ", "red"),
    "medium": ("bg_yellow", " 中 ", "yellow"),
    "low": ("bg_green", " 低 ", "green"),
}


_PRICE_COLS = [
    ("current", "当前价", "bold"),
    ("support", "支撑位", "green"),
    ("resistance", "压力位", "red"),
    ("stop_loss", "止损价", "red"),
    ("target", "目标价", "green"),
]


def format_dashboard_cli(data: DashboardData) -> str:
    lines: list[str] = []
    bg, badge_text, label, dc = _DECISION_META[data.decision_type]
    badge = _c(bg, _c("bold", badge_text))
    sent_label, sent_color = _sentiment_meta(data.sentiment_score)
    conf_pct = f"{data.confidence_level:.0%}"
    conf_filled = round(data.confidence_level * 10)
    conf_bar = "█" * conf_filled + "░" * (10 - conf_filled)

    # 标题
    lines.append("")
    lines.append(_c("cyan", _line("═")))
    lines.append(_c("bold", _center("📊 决策仪表盘")))
    lines.append(_c("dim", _center(data.quality_tag)))
    lines.append(_c("cyan", _line("═")))
    lines.append("")

    # 信号行
    lines.append(f"  {badge}  {_c(dc, label)}")
    lines.append(
        f"  {_c('bold', '信心')}: {_c(dc, conf_pct)} {conf_bar}    "
        f"{_c('bold', '情绪')}: {_c(sent_color, str(data.sentiment_score))} {sent_label}"
    )
    lines.append("")

    # 核心结论
    lines.append(f"  💬 {_c('bold', data.core_verdict)}")
    lines.append("")
    lines.append(f"  {_line('─')}")

    # 买卖点位
    if data.price_levels is not None:
        pl = data.price_levels
        cols = [(k, l, c) for k, l, c in _PRICE_COLS if k in pl]
        if cols:
            cw = 10
            lines.append(f"  🎯 {_c('bold', '买卖点位')}")
            lines.append("")
            sep = "┬".join("─" * cw for _ in cols)
            lines.append(f"  ┌{sep}┐")
            cw_inner = cw - 2
            hdr = "│".join(f" {_c(c, l.center(cw_inner))} " for _, l, c in cols)
            lines.append(f"  │{hdr}│")
            val = "│".join(f" {_c(c, str(pl[k]).center(cw_inner))} " for k, _, c in cols)
            lines.append(f"  │{val}│")
            bot = "┴".join("─" * cw for _ in cols)
            lines.append(f"  └{bot}┘")
            lines.append("")

    # 维度检查
    if len(data.checklist) >= 2:
        lines.append(f"  📋 {_c('bold', '维度检查')}")
        lines.append("")
        for item in data.checklist:
            icon, clr = _STATUS_META[item.status]
            lines.append(f"  {_c(clr, icon)} {_c('bold', item.dimension)}  {item.detail}")
        lines.append("")

    # 操作建议
    if data.split_advice is not None:
        sa = data.split_advice
        lines.append(f"  👤 {_c('bold', '操作建议')}")
        lines.append("")
        if "no_position" in sa:
            lines.append(f"  🆕 {_c('dim', '未持仓')}: {sa['no_position']}")
        if "has_position" in sa:
            lines.append(f"  💼 {_c('dim', '已持仓')}: {sa['has_position']}")
        lines.append("")

    # 风险优先级
    if data.risk_priority:
        lines.append(f"  🚨 {_c('bold', '风险优先级')}")
        lines.append("")
        for risk in data.risk_priority:
            bg_key, lvl_text, clr = _RISK_META[risk.level]
            tag = _c(bg_key, _c("bold", _c("white", lvl_text)))
            lines.append(f"  {tag} {_c(clr, risk.category)} — {risk.detail}")
            if risk.action:
                lines.append(f"       → {risk.action}")
        lines.append("")

    # 后续观察
    if data.next_watch:
        lines.append(f"  👀 {_c('bold', '后续观察')}")
        lines.append("")
        lines.append(f"  {' · '.join(data.next_watch)}")
        lines.append("")

    # 免责声明
    lines.append(f"  {_c('dim', _line('─'))}")
    lines.append("  ⚠️ 以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。")
    lines.append(_c("cyan", _line("═")))
    lines.append("")

    return "\n".join(lines)
