"""模板验证 CLI — 用真实数据验证报告模板与判读框架的有效性。

用法：
  python -m agents.analysis [template_id]              # 打印模板指导（数据契约+判读框架+输出结构）
  python -m agents.analysis [template_id] --data       # 按技能计划真实调用 mx_* skill 取数
  python -m agents.analysis [template_id] --report     # 用真实数据生成完整报告 + dashboard

template_id 默认 market_daily。--report 会先自动取数（等价 --data）。
"""
import asyncio
import json
import logging
import os
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")

# ── 加载 .env（与 config.py 一致的加载方式） ──
def _load_env():
    from config import Config  # noqa: F401  触发 Config 加载 .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _fmt(text: str, color: str = "cyan") -> str:
    _ANSI = {"cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "dim": "\033[2m", "reset": "\033[0m"}
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def print_guidance(template_id: str):
    """打印模板指导：数据契约 + 判读框架 + 输出结构 + 技能计划"""
    from agents.analysis.template_store import build_guidance, load_template

    t = load_template(template_id)
    print(_fmt(f"=== 模板: {t['id']}（{t.get('name', '')}）===", "cyan"))
    print("\n── 角色 ──")
    print(t.get("role", ""))
    print("\n── 数据契约 + 判读框架 + 输出结构 ──")
    print(build_guidance(t, "今天市场怎么样"))

    fw = t.get("analysis_framework")
    if not fw:
        print(_fmt("\n⚠️ 该模板没有 analysis_framework（分析判读框架）", "yellow"))

    print("\n── 技能计划 ──")
    for i, step in enumerate(t.get("skill_plan", []), 1):
        fallback = step.get("fallback_skills", [])
        fb = f"（fallback: {'、'.join(fallback)}）" if fallback else ""
        print(f"{i}. [{step.get('skill')}] {step.get('purpose', '')}{fb}")


# ── 真实取数 ──
_STOCK_QUERY_PATTERNS = [
    ("筹码分布", "{name}({code})的筹码分布数据"),
    ("资产负债", "{name}({code})的资产负债和财务结构数据"),
    ("行情", "{name}({code}) 最新价、涨跌幅、成交额、换手率、量比、PE、PB"),
    ("技术", "{name}({code}) 的MACD、RSI、均线、支撑位、压力位"),
    ("资金", "{name}({code}) 的主力资金流向、超大单、大单、北向资金"),
    ("财务", "{name}({code}) 的营业收入、净利润、ROE、毛利率、资产负债率"),
    ("持仓所属行业", "{name}({code}) 所属行业的代表资产走势"),
    ("行业周期", "{name}({code}) 所属行业的周期和政策新闻研报"),
    ("替代配置", "与{name}({code})同行业的替代配置候选股票"),
    ("估值", "{name}({code}) 的PE、PB和历史估值分位"),
]


def _resolve_stock_name(code: str) -> str:
    try:
        from utils.cache.market_data_db import get_stock_info
        info = get_stock_info(code)
        if info and info.get('name'):
            return info['name']
    except Exception:
        pass
    return code


def _inject_code(query: str, code: str, name: str) -> str:
    """把股票代码注入描述性查询，生成具体查询语句。"""
    for keyword, tmpl in _STOCK_QUERY_PATTERNS:
        if keyword in query:
            return tmpl.format(name=name, code=code)
    if "新闻" in query or "研报" in query:
        return f"{name}({code}) 最新公告、研报和新闻"
    if "前景" in query:
        return f"{name}({code}) 行业前景和发展前景"
    return query


def _inject_sector(query: str, sector: str) -> str:
    """把板块/标的名称注入描述性查询，生成具体查询语句。"""
    if "筛选" in query or "选股" in query or "概念股" in query:
        return f"{sector}概念股今日涨幅居前的龙头和跟风股"
    if "代表股" in query or "强势" in query:
        return f"{sector}板块今日涨幅居前的代表股"
    if "估值" in query or "资金" in query:
        return f"{sector}板块的估值和主力资金流向"
    if "政策" in query or "景气" in query:
        return f"{sector}板块最新政策、景气度和行业动态"
    if "新闻" in query or "风险" in query:
        return f"{sector}板块最新新闻、政策和风险提示"
    return f"{sector}板块{sector_verb(query)}"



def sector_verb(query: str) -> str:
    """从描述性查询里提取动作短语（去除'获取''个股/板块'等前缀）。"""
    for kw in ("获取", "查询", "的", "数据", "信息", "个股", "板块", "今日"):
        query = query.replace(kw, "")
    return query.strip() or "行情数据"


def _run_mx_skill(skill: str, query: str) -> str:
    """调用 mx_* skill，返回格式化文本。失败返回错误描述。"""
    try:
        if skill == "mx_data":
            from tools.other_skills.eastmoney.mx_data.mx_data import MXData
            client = MXData()
            result = client.query(query)
            tables, conds, total_rows, err = MXData.parse_result(result)
            if err:
                return f"查询失败: {err}"
            return MXData.format_terminal(result, tables, total_rows)
        if skill == "mx_xuangu":
            # 选股接口需要可执行条件，描述性 purpose 直接转为具体选股条件
            from tools.other_skills.eastmoney.mx_xuangu.mx_xuangu import MXSelectStock
            xuangu_query = query
            if "强势板块" in query or "代表股" in query:
                xuangu_query = "今日涨幅超过5%的股票"
            result = MXSelectStock().search(xuangu_query)
            rows, source, err = MXSelectStock.extract_data(result)
            if err:
                return f"查询失败: {err}"
            lines = [f"**选股结果**（{len(rows)}行，{source}）:"]
            for r in rows[:15]:
                lines.append(" | ".join(f"{k}:{v}" for k, v in list(r.items())[:6]))
            return "\n".join(lines)
        if skill == "mx_search":
            from tools.other_skills.eastmoney.mx_search.mx_search import MXSearch
            result = MXSearch().search(query)
            content = MXSearch.extract_content(result)
            return content or "（无内容）"
        return f"未知 skill: {skill}"
    except Exception as e:
        return f"调用异常: {e}"


async def fetch_data(template_id: str, code: str = "", sector: str = "") -> tuple[list, str]:
    """按技能计划取数，返回 (tool_calls, 汇总文本)。
    code 支持逗号分隔多标的（对比场景），每个标的独立跑一遍技能计划。"""
    from agents.analysis.template_store import load_template

    codes = [c.strip() for c in code.split(",") if c.strip()] if code else []
    names = {c: _resolve_stock_name(c) for c in codes}

    t = load_template(template_id)
    plan = t.get("skill_plan", [])
    if not plan:
        plan = [{"skill": "mx_data", "purpose": t.get("data_requirements", {}).get("default_query", "查询最新行情")}]

    print(_fmt(f"\n=== 按技能计划取数（{len(plan)} 步" + (f" × {len(codes)} 标的" if codes else "") + f"）===", "cyan"))
    if codes:
        print(_fmt(f"标的: {', '.join(f'{names[c]}({c})' for c in codes)}", "yellow"))
    elif sector:
        print(_fmt(f"板块: {sector}", "yellow"))
    tool_calls = []
    for i, step in enumerate(plan, 1):
        skill = step.get("skill", "mx_data")
        query = step.get("purpose", "")
        if not query:
            continue
        targets = codes or [""]
        for c in targets:
            q = query
            if c:
                q = _inject_code(q, c, names[c])
            elif sector:
                q = _inject_sector(q, sector)
            tag = f"（{names[c]}）" if c else ""
            print(f"\n{_fmt(f'[{i}] {skill}{tag}:', 'green')} {q}")
            output = _run_mx_skill(skill, q)
            print(_fmt(output[:600], "dim"))
            tool_calls.append({
                "tool_name": skill + (f":{c}" if c else ""),
                "tool_input": q,
                "tool_output": output,
            })

    summary = "\n".join(tc["tool_output"] for tc in tool_calls)
    return tool_calls, summary


async def gen_report(template_id: str, question: str, tool_calls: list) -> None:
    """走 run_analysis 生成报告，再生成 dashboard。"""
    from agents.analysis.dashboard_generator import generate as gen_dashboard
    from agents.analysis.engine import run_analysis
    from agents.analysis.evidence import build_bag
    from agents.analysis.extractors import extract_all
    from agents.analysis.models import AnalysisRequest
    from agents.analysis.report_builder import format_report
    from agents.analysis.template_store import evaluate, load_template

    log = logging.getLogger("analysis-cli")
    t = load_template(template_id)
    all_slots = set()
    for section in t.get("sections", []):
        all_slots.update(section.get("data_slots", []))
    bag = build_bag(tool_calls)
    slot_results = extract_all(bag, list(all_slots), {"logger": log})
    adjusted = evaluate(t, slot_results)
    filled = len(slot_results)
    total = len(all_slots)
    print(_fmt(f"\n=== 插槽提取: {filled}/{total}（{filled / total:.0%}）===", "cyan"))

    result = await run_analysis(
        AnalysisRequest(
            user_input=question,
            agent_name="analysis-cli",
            logger=log,
            template_id=template_id,
            selected_skills=["mx_data", "mx_search"],
        ),
        tool_calls=tool_calls,
        raw_result="",
    )
    print(_fmt("\n=== 生成报告 ===", "cyan"))
    print(f"（报告长度: {len(result.content)} 字符）")
    print(result.content)
    if result.degraded:
        print(_fmt("\n⚠️ 报告为降级模式（插槽数据稀疏）", "yellow"))
    if result.diagnostics:
        err = result.diagnostics.get("error")
        if err:
            print(_fmt(f"\n❌ 分析框架异常: {err}", "red"))

    # dashboard
    print(_fmt("\n=== 仪表盘 JSON ===", "cyan"))
    dashboard = await gen_dashboard(
        result.content,
        slot_results,
        template_id,
        question,
        scenario_tag=t.get("name", template_id),
        dashboard_id=t.get("dashboard_type"),
        logger_obj=log,
    )
    if dashboard:
        print(json.dumps(dashboard.model_dump(), ensure_ascii=False, indent=2)[:4000])
    else:
        print(_fmt("（dashboard 生成失败）", "yellow"))


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    template_id = args[0] if args else "market_daily"
    question = "今日 A 股市场整体表现如何，主线板块是什么，明天需要注意什么"

    code = ""
    if "--code" in flags:
        try:
            code = sys.argv[sys.argv.index("--code") + 1]
        except IndexError:
            pass
    if code:
        question = f"分析一下 {code} 这只股票，现在能不能买，给出操作建议和风险"

    sector = ""
    if "--sector" in flags:
        try:
            sector = sys.argv[sys.argv.index("--sector") + 1]
        except IndexError:
            pass
    if sector and not code:
        question = f"分析一下 {sector} 板块，现在能不能买，给出操作建议和风险"

    if "--question" in flags:
        try:
            idx = sys.argv.index("--question")
            question = sys.argv[idx + 1]
        except IndexError:
            pass

    print_guidance(template_id)

    tool_calls = []
    if "--data" in flags or "--report" in flags:
        tool_calls, _ = await fetch_data(template_id, code=code, sector=sector)

    if "--report" in flags:
        await gen_report(template_id, question, tool_calls)
    else:
        print(_fmt("\n提示: 加 --data 真实取数；加 --report 生成完整报告+dashboard", "dim"))


if __name__ == "__main__":
    _load_env()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
    except Exception:
        traceback.print_exc()
