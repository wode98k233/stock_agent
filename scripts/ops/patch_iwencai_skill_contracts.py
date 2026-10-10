# -*- coding: utf-8 -*-
"""为同花顺问财（10jqka）技能包补齐技能注册契约段。

背景
----
`tools/skill_register.py` 从每个技能包的 SKILL.md 解析三段结构：
  - `## 工具列表`   -> `### 工具N：<tool_name>` + `- 功能：...` + `  - <参数>: <类型>（必填|可选）- ...`
  - `## 目录层信息` -> `- 关键参数：a、b` / `- 核心目标：...`
  - `## 使用指南`   -> 作为 usage_guide 注入 LLM 上下文

SkillHub 官方下载包只带 `## 技能概述` / `## 核心处理流程`，缺前两段；且官方文档里的
工具名是包内 CLI 脚本名，与本项目 `skills.py` 收敛后的统一工具名（iwc_query /
iwc_search）不一致。若不补齐：

  * `_parse_tool_list` 返回空 -> 技能目录（get_catalog_text）不列出任何工具
  * `_filter_tool_registry_for_skill` 退化为「保留全量 registry」
  * 管理页每个技能显示 0 个工具

本脚本按 `DOMAIN_META` 表为 16 个技能包重写 `## 工具列表` / `## 目录层信息`，
并在缺失时生成 `## 使用指南`。幂等：重复执行结果一致。

用法:
    python scripts/ops/patch_iwencai_skill_contracts.py            # 应用
    python scripts/ops/patch_iwencai_skill_contracts.py --check    # 只报告差异
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS_ROOT = os.path.join(REPO_ROOT, "tools", "other_skills", "10jqka")

TOOL_QUERY = "iwc_query"
TOOL_SEARCH = "iwc_search"

# slug -> 契约元数据
#   tool      : 统一工具名
#   channel   : iwc_search 专用（news / announcement / report）
#   category  : frontmatter category
#   fn_desc   : `- 功能：` 文案
#   example   : 参数说明里的示例问句
#   target    : `- 核心目标：` 文案
#   scenes    : `### 适用场景` 条目
#   calls     : `### 典型调用` 里的问句（工具名自动补）
DOMAIN_META = {
    "hithink-astock-selector": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="A 股多条件组合选股：按行情指标、技术形态、财务指标、行业概念等条件筛选个股",
        example="市盈率小于20且市值大于100亿的股票",
        target="把选股想法转成自然语言问句，返回符合条件的 A 股清单",
        scenes=["按财务条件筛股（PE/PB/ROE/负债率）", "按行情条件筛股（涨跌幅/成交额/换手率）",
                "按技术形态筛股（均线多头/突破新高/MACD 金叉）", "按行业概念筛股（科技/医药/消费）"],
        calls=["市盈率小于20且市值大于100亿的股票", "今日涨跌幅大于5%的A股", "均线多头排列的股票",
               "属于科技股的股票"],
    ),
    "hithink-market-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="获取股票、ETF、指数等实时价格、涨跌幅、成交量、主力资金流向、大小单、技术指标等行情数据",
        example="同花顺最新价格",
        target="查询标的行情快照与资金流向，返回结构化行情表格",
        scenes=["个股/ETF/指数最新价与涨跌幅", "成交量、成交额、换手率", "主力资金流向与大小单",
                "技术指标（MACD/KDJ/RSI/布林线）"],
        calls=["同花顺最新价格", "上证指数行情", "主力资金净流入排名", "MACD金叉的股票"],
    ),
    "hithink-finance-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询全市场个股营业收入、净利润、ROE、负债率、现金流、毛利率、净利率等财务指标",
        example="贵州茅台近三年ROE",
        target="按标的与报告期取财务指标，返回结构化财务数据表格",
        scenes=["单标的多年财务指标对比", "全市场财务指标排序与筛选", "盈利质量（毛利率/净利率/现金流）",
                "偿债与杠杆（负债率/流动比率）"],
        calls=["贵州茅台近三年ROE", "净利润同比增长超过50%的公司", "资产负债率低于30%的股票",
               "毛利率连续三年提升的公司"],
    ),
    "hithink-zhishu-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询上证指数、沪深300、创业板指、恒生指数、纳斯达克指数等指数行情与点位数据",
        example="上证指数最新点位",
        target="查询指数行情、涨跌幅、成交量、点位，返回结构化指数数据",
        scenes=["主要宽基指数实时行情", "指数区间涨跌幅", "指数成交量与点位"],
        calls=["上证指数最新点位", "沪深300今年以来涨跌幅", "创业板指行情"],
    ),
    "hithink-industry-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询行业估值、财务、盈利、行情、板块排名等数据",
        example="银行行业平均市盈率",
        target="按行业取估值/财务/行情并横向排名，返回结构化行业数据",
        scenes=["行业估值水平与分位", "行业涨跌幅与资金流向排名", "行业财务与盈利指标"],
        calls=["银行行业平均市盈率", "各行业涨跌幅排名", "行业主力资金净流入排名"],
    ),
    "hithink-macro-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询 GDP、CPI、PPI、利率、汇率、社融、M2、PMI、工业增加值等宏观经济指标",
        example="最新CPI同比",
        target="取宏观时间序列指标，返回结构化宏观数据",
        scenes=["通胀指标（CPI/PPI）", "货币与信贷（M2/社融/利率）", "景气指标（PMI/工业增加值）",
                "汇率与进出口"],
        calls=["最新CPI同比", "近12个月社融存量同比", "制造业PMI最新值", "人民币对美元中间价"],
    ),
    "hithink-event-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询个股业绩预告、增发、股权质押、限售解禁、机构调研、监管函等事件数据",
        example="本周限售解禁的股票",
        target="取个股事件日历与明细，返回结构化事件数据",
        scenes=["业绩预告与业绩快报", "限售解禁与股权质押", "增发配股与回购", "机构调研与监管函"],
        calls=["本周限售解禁的股票", "发布业绩预增公告的公司", "股权质押比例超过50%的股票",
               "近一个月被机构调研的公司"],
    ),
    "hithink-business-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询主营业务构成、主要客户、供应商、参控股公司、股权投资、重大合同等经营数据",
        example="贵州茅台主营业务构成",
        target="取公司经营层面的结构数据，返回结构化经营数据",
        scenes=["主营业务构成与收入拆分", "主要客户与供应商集中度", "参控股公司与股权投资",
                "重大合同与订单"],
        calls=["贵州茅台主营业务构成", "前五大客户营收占比超过50%的公司", "参股芯片公司的上市公司"],
    ),
    "hithink-basicinfo-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询全品类标的（股票/指数/基金/期货/期权/转债/债券等）的基础信息、发行主体、"
                "机构资料、费率、上市地点、上市日期等静态信息",
        example="宁德时代上市日期",
        target="取标的静态基础资料，返回结构化基本资料",
        scenes=["上市日期/上市地点/所属行业", "发行主体与机构资料", "基金费率与份额结构"],
        calls=["宁德时代上市日期", "贵州茅台所属申万行业", "某ETF的管理费率"],
    ),
    "hithink-insresearch-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询研报评级、业绩预测、ESG 评级、信用评级、主体评级、基金评级、券商金股等机构观点数据",
        example="贵州茅台机构评级和目标价",
        target="取机构一致预期与评级信息，返回结构化机构研究数据",
        scenes=["机构评级与目标价", "盈利预测与一致预期", "ESG 与信用评级", "券商月度金股"],
        calls=["贵州茅台机构评级和目标价", "2026年一致预期净利润上调的公司", "本月券商金股名单"],
    ),
    "hithink-management-query": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="查询股本结构、股权结构、股东户数、前十大股东/流通股东、主要持有人、实际控制人等股权信息",
        example="贵州茅台前十大股东",
        target="取股本与股东结构数据，返回结构化股权信息",
        scenes=["股本结构与限售股本", "前十大股东与流通股东", "股东户数与筹码集中度", "实际控制人"],
        calls=["贵州茅台前十大股东", "股东户数环比下降的公司", "实际控制人为国资委的上市公司"],
    ),
    "hithink-sector-selector": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="按行业估值、资金流向、涨跌幅、板块类型等条件组合筛选市场板块",
        example="涨幅前五的行业板块",
        target="按条件筛选板块并返回排序结果",
        scenes=["板块涨跌幅排序", "板块资金流向排序", "板块估值水平筛选", "概念板块筛选"],
        calls=["涨幅前五的行业板块", "资金净流入的行业板块", "估值分位低于30%的板块"],
    ),
    "hithink-etf-selector": dict(
        tool=TOOL_QUERY, category="数据查询",
        fn_desc="按行情、跟踪指数基本面、规模、风格类型等条件筛选 ETF",
        example="规模大于100亿的宽基ETF",
        target="按条件筛选 ETF 并返回排序结果",
        scenes=["按规模与流动性筛 ETF", "按跟踪指数与风格筛 ETF", "按费率筛 ETF"],
        calls=["规模大于100亿的宽基ETF", "跟踪中证红利的ETF", "费率低于0.2%的股票ETF"],
    ),
    "news-search": dict(
        tool=TOOL_SEARCH, channel="news", category="资讯搜索",
        fn_desc="财经资讯搜索：覆盖官媒、主流财经媒体、垂直行业网站、公司官网，返回标题、摘要、链接",
        example="贵州茅台今日新闻",
        target="搜索财经新闻与政策动态，返回结构化资讯列表",
        scenes=["个股/行业最新新闻", "政策与监管动态", "行业趋势与企业业务进展"],
        calls=["贵州茅台今日新闻", "半导体行业最新政策", "新能源汽车销量新闻"],
        relation="与东财 mx_search 功能重叠，可互为备用（额度与信源互补）",
    ),
    "announcement-search": dict(
        tool=TOOL_SEARCH, channel="announcement", category="资讯搜索",
        fn_desc="公告搜索：支持 A 股、港股、基金、ETF 等标的公告，覆盖定期报告、分红派息、"
                "回购增持、资产重组等类型",
        example="贵州茅台 分红公告",
        target="搜索上市公司公告，返回结构化公告列表",
        scenes=["定期财务报告与业绩预告", "分红派息与回购增持", "资产重组与重大合同", "股东与治理类公告"],
        calls=["贵州茅台 分红公告", "比亚迪2025年年报", "上市公司回购增持公告", "资产重组公告"],
        relation="与东财 mx_search 的公告搜索互补，可互为备用",
    ),
    "report-search": dict(
        tool=TOOL_SEARCH, channel="report", category="资讯搜索",
        fn_desc="研报搜索：收录主流投研机构研究报告，返回标题、摘要、链接，摘要含盈利预测与估值指标",
        example="贵州茅台研报",
        target="搜索卖方研究报告，返回结构化研报列表",
        scenes=["个股深度研报与首次覆盖", "行业策略与专题研究", "盈利预测与评级调整"],
        calls=["贵州茅台研报", "白酒行业策略报告", "半导体行业深度研报"],
    ),
}

MARKER = "<!-- iwencai-contract: auto-generated by scripts/ops/patch_iwencai_skill_contracts.py -->"
SECTION_RE = re.compile(r"^##\s*(工具列表|目录层信息|使用指南)\s*$")


def _split_frontmatter(text: str):
    """返回 (frontmatter_lines, body)。缺 frontmatter 时返回 ([], text)。"""
    norm = text.lstrip("\ufeff")
    if not norm.startswith("---"):
        return [], norm
    parts = norm.split("\n---", 2)
    if len(parts) < 2:
        return [], norm
    fm_body = parts[0][3:]
    rest = parts[1]
    # rest 以 "\n" 开头（来自 "\n---"），其后为正文
    tail = parts[2] if len(parts) > 2 else ""
    body = (rest[1:] if rest.startswith("\n") else rest) + tail
    return [ln for ln in fm_body.split("\n")], body


def _ensure_frontmatter(lines, meta):
    """补齐 version / category / required_env_vars，保留其它键。

    缩进续行（如 `required_env_vars:` 之下的 `  - IWENCAI_API_KEY`）归属其父键；
    父键属于受管键时直接丢弃，避免重复输出。
    """
    want_order = ["name", "display_name", "description", "version", "category", "required_env_vars", "license"]
    have = {}
    extras = []
    parent = None
    for ln in lines:
        if not ln.strip():
            continue
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", ln)
        if m:
            parent = m.group(1)
            have[parent] = m.group(2)
            continue
        # 缩进续行
        if parent in want_order:
            continue
        extras.append((parent, ln))

    have.setdefault("version", "1.0.0")
    have["category"] = meta["category"]

    out = []
    for key in want_order:
        if key not in have:
            continue
        if key == "required_env_vars":
            out.append("required_env_vars:")
            out.append("  - IWENCAI_API_KEY")
        else:
            out.append(f"{key}: {have[key]}")
    for k, v in have.items():
        if k not in want_order and k is not None:
            out.append(f"{k}: {v}")
    out.extend(v for _, v in extras)
    return out


def _strip_generated_sections(body: str) -> str:
    """删除已有的 工具列表 / 目录层信息 / 使用指南 段（保留其余正文）。"""
    lines = body.split("\n")
    out = []
    skip = False
    for ln in lines:
        if SECTION_RE.match(ln):
            skip = True
            continue
        if skip and ln.startswith("## "):
            skip = False
        if not skip:
            out.append(ln)
    text = "\n".join(out)
    text = text.replace(MARKER, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _build_sections(slug: str, meta: dict) -> str:
    tool = meta["tool"]
    if tool == TOOL_SEARCH:
        params = [
            f"  - query: str（必填）- 搜索问句，如\"{meta['example']}\"",
            f"  - channel: str（可选）- 搜索渠道，本技能固定为 {meta['channel']}，默认 {meta['channel']}",
            "  - size: int（可选）- 返回条数，默认 10",
        ]
        entry = TOOL_SEARCH
        key_params = "搜索问句（必填）、channel（news/announcement/report）、size（返回条数）"
        notes = [
            f"- 本技能走搜索通道，channel 固定为 `{meta['channel']}`；换渠道请改用对应技能",
            "- 返回 `data` 数组，元素含 title / summary / url，摘要里可能带盈利预测与估值表格",
            "- 数据来源为同花顺问财，引用时请注明来源",
        ]
    else:
        params = [
            f"  - query: str（必填）- 自然语言查询问句，如\"{meta['example']}\"",
            "  - page: str（可选）- 页码，从 1 开始，默认 1",
            "  - limit: str（可选）- 每页条数，默认 10",
        ]
        entry = TOOL_QUERY
        key_params = "自然语言问句（必填）、page（页码）、limit（每页条数）"
        notes = [
            "- 网关按 `query` 文本路由，不用切换工具；条件越具体结果越精准",
            "- 返回 `columns` + `datas`，字段名随问句变化（含日期后缀如 `市盈率(pe,ttm)[20260918]`），需动态解析",
            "- `datas` 默认 10 条，`code_count` 是命中总数，必要时用 page/limit 翻页",
            "- 空结果时最多放宽重试 2 次，并在回答里说明最终使用的问句",
            "- 数据来源为同花顺问财，引用时请注明来源",
        ]

    lines = [
        MARKER,
        "## 工具列表",
        "",
        f"### 工具1：{tool}",
        f"- 功能：{meta['fn_desc']}",
        f"- 调用入口：{entry}",
        *params,
        "",
        "## 目录层信息",
        f"- 关键参数：{key_params}",
        f"- 核心目标：{meta['target']}",
        "",
        "## 使用指南",
        "",
        "### 适用场景",
        *[f"- {s}" for s in meta["scenes"]],
    ]
    if meta.get("relation"):
        lines.append(f"- {meta['relation']}")
    lines += [
        "",
        "### 典型调用",
        *[f"- {tool}(\"{c}\")" for c in meta["calls"]],
        "",
        "### 注意事项",
        *notes,
        "- 问财 SkillHub 文档标注为「每日免费额度 100 次/skill」；实测网关不校验 "
        "`X-Claw-Skill-Id`，按 skill 独立计数一说无法从外部证实，请按总额度保守使用",
        "",
    ]
    return "\n".join(lines)


def patch_one(slug: str, meta: dict, check_only: bool) -> tuple[bool, str]:
    skill_dir = os.path.join(SKILLS_ROOT, slug)
    md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(md_path):
        return False, f"MISSING {slug}/SKILL.md"

    raw = io.open(md_path, encoding="utf-8-sig", errors="ignore").read()
    fm_lines, body = _split_frontmatter(raw)
    name_match = re.search(r"^name:\s*(.+)$", "\n".join(fm_lines), re.M)
    if not name_match or name_match.group(1).strip() != slug:
        return False, f"NAME-MISMATCH {slug} vs {name_match.group(1).strip() if name_match else 'NONE'}"

    new_fm = _ensure_frontmatter(fm_lines, meta)
    stripped = _strip_generated_sections(body)

    # 标题（# ...）之后插入契约段，标题保留在最前
    title = ""
    rest = stripped
    m = re.match(r"^(#\s+[^\n]*)\n", stripped)
    if m:
        title = m.group(1)
        rest = stripped[m.end():]

    new_text = "---\n" + "\n".join(new_fm) + "\n---\n\n"
    if title:
        new_text += title + "\n\n"
    new_text += _build_sections(slug, meta) + "\n" + rest.strip() + "\n"

    if new_text == raw:
        return True, f"OK-unchanged {slug}"

    if check_only:
        return True, f"DIFF {slug} ({len(raw)} -> {len(new_text)} B)"

    io.open(md_path, "w", encoding="utf-8", newline="\n").write(new_text)
    return True, f"PATCHED {slug} ({len(raw)} -> {len(new_text)} B)"


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐问财技能包的注册契约段")
    ap.add_argument("--check", action="store_true", help="只报告差异，不写文件")
    args = ap.parse_args()

    ok = True
    for slug in sorted(DOMAIN_META):
        good, msg = patch_one(slug, DOMAIN_META[slug], args.check)
        ok = ok and good
        print(msg)

    # 反向检查：目录里有 SKILL.md 但不在 DOMAIN_META 中的包
    if os.path.isdir(SKILLS_ROOT):
        for d in sorted(os.listdir(SKILLS_ROOT)):
            p = os.path.join(SKILLS_ROOT, d)
            if (os.path.isdir(p) and not d.startswith(".") and d != "__pycache__"
                    and os.path.exists(os.path.join(p, "SKILL.md")) and d not in DOMAIN_META):
                print(f"UNMAPPED {d}/SKILL.md (未在 DOMAIN_META 登记)")
                ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
