"""
分析框架引擎 — 报告合成器

快速模式：1 次 LLM 调用生成整份报告。
规则化兜底：预算不足时确定性拼接。
"""
import json
import logging
import re
from typing import Any, Optional

from agents.analysis.evidence_view import _text, _tool_items, tool_output_status
from agents.analysis.models import SlotResult, SectionPlan, ReportSection
from config import Config
from utils.budget import BudgetExceeded

logger = logging.getLogger(__name__)


def _min_report_tokens(template: dict | None = None) -> int:
    """报告最低 token 目标，支持运行时配置，并允许**模板级覆盖**。

    模板可用 `min_report_tokens` 覆盖全局 `REPORT_MIN_TOKENS`（默认 1500）。
    **设 0 表示"轻量模板"**：不触发 `_expand_report_if_short` 扩写，且提示词里
    把「全文目标不少于 N token」换成「简洁优先、只答用户所问」。
    背景（2026-10-01）：`max_report_words` 只被模板管理界面消费、生成路径不读，
    因此单靠模板字段无法让报告变短 —— 用户明明说「只需要这个就行了」，
    报告仍会被扩写机制拉到 3500 token 量级（实测 7283 字符）。
    2026-10-10 下调 3500→2200；2026-10-18 再下调至 1500（下限 + 扩写指令
    「必须增加表格/矩阵/Mermaid」是报告注水的直接来源）。
    """
    if template is not None and template.get("min_report_tokens") is not None:
        try:
            return max(0, int(template["min_report_tokens"]))
        except (TypeError, ValueError):
            pass
    return max(0, int(getattr(Config, "REPORT_MIN_TOKENS", 1500) or 0))


def _max_report_words(template: dict | None = None) -> int:
    """模板声明的全文上限（字），0 表示未声明。生成路径消费，不再只是界面字段。"""
    if template:
        try:
            return max(0, int(template.get("max_report_words") or 0))
        except (TypeError, ValueError):
            pass
    return 0


# ═══════════════════════════════════════════════════════════════
# 报告图表归集辅助：去重 + 技术面图板注入
# ═══════════════════════════════════════════════════════════════

def _dedupe_chart_markers(markers: list) -> list:
    """按 (stock_code, chart_type, title) 去重，同组保留最后出现的（最新）。

    为什么带上 title（2026-11-02）：图表萃取放宽到 iwc_query 后，**一次调用会产出
    多张同 chart_type 的对比图**（涨跌幅 / 估值 / 盈利），若仍按 (code, type) 去重，
    同一 (code='', comparison) 只会剩最后一张——用户的观感就是「还是没图」。
    顺带修掉 AGENTS.md §7 记录的老问题：RSI 与 MACD 同属 indicator，同股票只剩 MACD。
    前端按**标题关键词**分组出 Tab（`chat-core.js::_groupKey`），同类型多张可以并排渲染。

    查不到元数据的 ID 原样保留，绝不因去重丢图。
    """
    if not markers:
        return markers
    try:
        from agents.analysis.chart_extractor import get_fragment
        seen: dict = {}          # (code, type, title) -> marker（后出现的覆盖前面的）
        fallback: list = []      # 查不到元数据的标记，原样保留
        for m in markers:
            cid = re.sub(r"^@@CHART:|@@$", "", m)
            frag = get_fragment(cid) if cid else None
            if not frag:
                fallback.append((m, len(seen)))
                continue
            key = (frag.get("stock_code") or "", frag.get("chart_type") or "",
                   frag.get("title") or "")
            seen[key] = m
        # fallback 按原顺序插入，且不与已保留的标记重复
        result = list(seen.values())
        for m, pos in fallback:
            if m not in result:
                result.insert(min(pos, len(result)), m)
        return result
    except Exception:
        logger.debug("report_builder: 图表去重失败，保留原标记", exc_info=True)
        return markers


# 工具证据中的 6 位股票/ETF 代码（避免被更长数字串包含）
_EVIDENCE_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 合法 A 股/ETF 代码前缀（沪 6/9/51/52/56/58，深 0/3/15/16/18，北 4/8）
_VALID_CODE_PREFIX = ("6", "9", "5", "0", "3", "1", "4", "8")


def _is_plausible_stock_code(code: str) -> bool:
    """粗校验：6 位且前缀像 A 股/ETF 代码，滤掉证据里的金额/数量等杂数字。"""
    return bool(re.fullmatch(r"\d{6}", code)) and code[0] in _VALID_CODE_PREFIX


def _tech_panel_max() -> int:
    """报告技术面图板数量上限（可经 .env / 页面配置面板调整，0=不注入）。"""
    try:
        return max(0, int(getattr(Config, "REPORT_TECH_PANEL_MAX", 3)))
    except Exception:
        return 3


# 显式例外：即使模板自描述为市场类，也不按指数解释（扩展口，当前为空）
_STOCK_FIRST_TEMPLATE_IDS: frozenset = frozenset()


def _prefer_index_symbols(template: dict) -> bool:
    """该报告的「标的类型」是否应按 A 股指数解释。

    依据模板自描述字段（`agents/report_templates/*/template.json`）：
    `asset_type == 'market'`（标的是市场整体）且 `intent_type == 'daily_summary'`
    → market_daily（市场日报）/ macro_daily（经济日报）。

    这类报告里出现的同码标的必然指指数，而不是同码个股——实测
    `index_info ∩ stock_info` 只有一个 **000001**（上证指数 ↔ 平安银行）。
    2026-09-30「今日股市总结」把平安银行当主图注入，根因就是缺这一层判定。

    注意：`asset_type='market'` 但不做限制的还有 event_impact / hot_news——
    那两类报告的主标的是具体事件/个股，不在此列。
    """
    tid = str((template or {}).get("id") or "")
    if tid and tid in _STOCK_FIRST_TEMPLATE_IDS:
        return False
    return ((template or {}).get("asset_type") == "market"
            and (template or {}).get("intent_type") == "daily_summary")


def _is_index_code(code: str) -> bool:
    """该码是否存在于指数表（异常一律按 False 处理）。"""
    try:
        from utils.cache.market_data_db import is_index_code
        return bool(is_index_code(str(code)))
    except Exception:
        return False


def _index_symbols(codes: list) -> Optional[list]:
    """把候选码收敛为「指数码」；判定通道不可用时返回 None（调用方回退原行为，
    不能因一次 DB 异常就把市场类报告的技术面图板全部吞掉）。"""
    try:
        from utils.cache.market_data_db import is_index_code
        return [c for c in codes if is_index_code(str(c))]
    except Exception:
        logger.debug("report_builder: 指数码判定失败，回退原行为", exc_info=True)
        return None


def _maybe_build_tech_panels(chart_markers: list, tool_evidence: str = "",
                             prefer_index_symbols: bool = False,
                             code_hint: str = "") -> list:
    """提取涉及的主标的（按出现频次前 N），每个尝试生成一张技术面图板。

    板块分析场景：报告覆盖多只个股时，每只各有一张图板（上限 REPORT_TECH_PANEL_MAX）。
    主 code 来源优先级：
      1. 图表元数据中出现频次最高的 stock_code（有基础图时）
      2. 工具证据中高频出现的 6 位代码（无基础图时回退——如 ETF 查询
         妙想未返回指标表，报告一张图都没有的场景）
      3. ``code_hint``：fork 路径从对话（用户提问 + 工具入参）回收的标的线索。
         ⚠️ 为什么需要这一层（2026-11-02）：fork 路径传给 ``tool_evidence`` 的
         只有「含 @@CHART 标记」的消息，一旦上游没产出任何图表片段（数据源换了 /
         妙想额度耗尽），evidence 必为空串 → 这条独立兜底也被一起掐断，
         报告连一张本地 K 线图板都没有。

    prefer_index_symbols=True（市场日报类报告，见 `_prefer_index_symbols`）：
      候选**收敛为指数码**，且歧义码（000001）显式按 INDEX 解释。这类报告的
      技术面图板应当是大盘指数；找不到指数码就不注入，绝不拿证据里的高频
      个股/ETF 码顶替（那正是平安银行被注入的路径）。

    本地 K 线数据不足、查不到或任何异常 → 返回 []（报告保持原样，绝不抛错）。
    """
    if not chart_markers and not tool_evidence and not code_hint:
        return []
    try:
        from collections import Counter
        from agents.analysis.chart_extractor import get_fragment, ensure_tech_panel

        max_panels = _tech_panel_max()
        if max_panels <= 0:
            return []

        codes = []
        names = {}
        for m in chart_markers or []:
            cid = re.sub(r"^@@CHART:|@@$", "", m)
            frag = get_fragment(cid) if cid else None
            if frag and frag.get("stock_code") and frag.get("chart_type") != "tech_panel":
                code = str(frag["stock_code"])
                codes.append(code)
                if code not in names and frag.get("stock_name"):
                    names[code] = str(frag["stock_name"])

        if not codes:
            # 回退：从工具证据 / 对话线索里提取高频 6 位代码
            # （mx_data query 里 code 会多次出现；iwc_query 的入参同理）
            for text in (tool_evidence, code_hint):
                if not text:
                    continue
                found = [c for c in _EVIDENCE_CODE_RE.findall(text)
                         if _is_plausible_stock_code(c)]
                if found:
                    codes = found
                    break

        if not codes:
            return []

        if prefer_index_symbols:
            index_codes = _index_symbols(codes)
            if index_codes is None:
                prefer_index_symbols = False     # 判定不可用 → 回退原行为
            else:
                codes = index_codes
                if not codes:
                    return []

        panels = []
        for main_code, count in Counter(codes).most_common(max_panels):
            is_idx = prefer_index_symbols and _is_index_code(main_code)
            # 指数走 ensure_tech_panel 内部自己的形态校验（H11001/HSTECH 等带字母）
            if not is_idx and not _is_plausible_stock_code(main_code):
                continue
            panel_id = ensure_tech_panel(main_code, name=names.get(main_code, ""),
                                         market="INDEX" if is_idx else None)
            if panel_id:
                # 注意：模块级 logger 是标准 logging（非 RadarLogger），
                # 不能用 log.info("A", ...) 的分类标签风格
                logger.info("技术面图板已注入: %s(%s) 类型=%s 主图来源=%d张",
                            names.get(main_code) or main_code, main_code,
                            "INDEX" if is_idx else "STOCK", count)
                panels.append(f"@@CHART:{panel_id}@@")   # 统一带壳
        return panels
    except Exception:
        logger.debug("report_builder: 技术面图板生成失败（跳过）", exc_info=True)
        return []


_DASHBOARD_BLOCK_RE = re.compile(r"@@DASHBOARD_START@@.*?@@DASHBOARD_END@@", re.S)


def strip_copied_blocks(content: str) -> str:
    """清掉 LLM 从上下文「续写」进正文的标记块（仪表盘 JSON / 图表 UUID）。

    2026-11-06 事故：fork 路径把完整 agent 对话当前缀，而对话里某条 AI 消息的尾部
    正是**上一份报告**的 `@@CHART:…@@ + 免责声明 + @@DASHBOARD_START@@{…}`。
    report-fast 写完正文后照着「续写」了这段尾巴：

    - 正文里多出一坨**旧仪表盘 JSON**（前端只能渲染第一个块，第二个就裸露成文字）；
    - 前端渲染的是旧块，字段对不上新 schema → 卡片显示 `undefined`；
    - 那些旧 `@@CHART@@` UUID 还会被当成「本轮图表」收进报告尾部（重复、指向旧图）。

    所以正文里出现的这两类标记**一律不可信、一律清除**：图表只认工具证据
    （`tool_evidence`），仪表盘只认 `dashboard_node` 生成的最后一个块。
    """
    if not content:
        return content or ""
    text = _DASHBOARD_BLOCK_RE.sub("", content)
    # LLM 复述的图表标记（完整壳）整体删除；裸 UUID 交给调用方按已知 id 清
    text = re.sub(r"\s*@@CHART:[0-9a-f-]+@@\s*", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip()


def _merge_chart_markers(content: str, tool_evidence: str = "",
                         template: dict = None, code_hint: str = "") -> str:
    """归集图表标记到报告末尾（统一入口，任何异常返回原 content）。

    1) 只从 `tool_evidence` 收集图表 id（正文复述的一律不采信，见 `strip_copied_blocks`）；
    2) 清掉正文里 LLM 复述的仪表盘块 / 图表标记（含抄坏的裸 UUID 残留）；
    3) 尝试注入「技术面图板」增强卡（`template` 决定标的类型：市场日报类按指数；
       `code_hint` 是无图表片段时的标的线索，见 `_maybe_build_tech_panels`）；
    4) 统一以完整 ``@@CHART:id@@`` 格式拼到报告末尾。
    """
    try:
        # 先清掉 LLM 续写的旧块：既避免旧仪表盘 JSON 裸露进正文，
        # 也避免把上一轮的 UUID 当成本轮图表。
        known_ids = set(re.findall(r"@@CHART:([0-9a-f-]+)@@", content or ""))
        content = strip_copied_blocks(content)

        markers = re.findall(r"@@CHART:[0-9a-f-]+@@", tool_evidence or "")
        unique = _dedupe_chart_markers(list(dict.fromkeys(markers)))

        # 清理正文里「丢壳」的裸 UUID（LLM 抄坏的情形），只删已知 id，不误伤正文数字
        if known_ids:
            for cid in known_ids:
                content = content.replace(cid, " ")
            content = re.sub(r"[ \t]+\n", "\n", content)
            content = re.sub(r"\n{3,}", "\n\n", content).rstrip()

        # 尝试注入技术面图板（支持多标的，板块分析每只个股各一张；失败静默）
        panels = _maybe_build_tech_panels(
            unique, tool_evidence=tool_evidence, code_hint=code_hint,
            prefer_index_symbols=_prefer_index_symbols(template or {}),
        )
        unique.extend(panels)

        if unique:
            # 上限保护：一次 run 里 iwc_query 可能被调十几次，每次都可能产出对比图，
            # 不设上限会把报告尾部堆成图表墙（正文被挤没）。图板优先保留，
            # 基础图只留最近 N 张（越靠后越接近最终结论）。
            keep = _MAX_REPORT_CHARTS
            if keep > 0 and len(unique) > keep:
                panels_kept = panels[-keep:]
                room = max(0, keep - len(panels_kept))
                base = [m for m in unique if m not in panels]
                unique = (base[len(base) - room:] if room else []) + panels_kept
            content = content.rstrip() + "\n\n" + " ".join(unique)
        return content
    except Exception:
        logger.debug("report_builder: 图表标记归集失败，保留原 content", exc_info=True)
        return content


_CHART_MARK_RE = re.compile(r"@@CHART:[0-9a-f-]+@@")

# 报告尾部图表总上限：技术面图板优先，基础图只留最近几张。
# 取值参照历史上「一份大盘日报 ≈ 10 张图」的既有观感（10-30 那次 run 落 10 条片段）。
_MAX_REPORT_CHARTS = 10


def _chart_evidence_from_conversation(conversation) -> str:
    """从 agent 对话里回收图表标记（fork 路径专用）。

    ⚠️ 为什么必须有：图表标记是 ``agents/react/tool_node_base.py`` 追加到**工具消息正文**
    里的（``📊 已自动生成图表: N 张 @@CHART:uuid@@``），而 fork 路径（PRD-A9 §14）把报告
    prompt 直接拼在对话尾部，不再单独传 ``tool_evidence``。若不在这里回收，
    ``_merge_chart_markers`` 只能看到 LLM **自己复述**的标记——而 LLM 几乎从不抄这串 UUID，
    结果就是每份走 fork 的报告都零图表（2026-10-14 引入 fork 后实测：10-11 之后再无
    ``@@CHART``；10-30 那次 run 里 chart_fragments 明明落了 9 条，报告正文却一张没有）。

    只回收**含标记**的消息，不把整轮工具输出搬进内存；任何异常都退化成空串（不阻断出报告）。
    """
    if not conversation:
        return ""
    parts: list[str] = []
    try:
        for msg in conversation:
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, list):      # 多模态分段：拼 text 片段
                content = " ".join(
                    str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
                )
            if not content:
                continue
            text = str(content)
            if _CHART_MARK_RE.search(text):
                parts.append(text)
    except Exception:
        logger.debug("report_builder: 从对话回收图表标记失败，退回空证据", exc_info=True)
    return "\n".join(parts)


# 对话线索取样上限：只用于正则捞 6 位代码，没必要把整轮对话搬进内存
_SYMBOL_HINT_MAX_CHARS = 20000


def _conversation_symbol_hints(conversation) -> str:
    """从对话里回收「标的线索」（用户提问 + 工具入参），供技术面图板选主图。

    与 ``_chart_evidence_from_conversation`` 的分工必须分清楚：

    - 那个函数只回收**含 @@CHART 标记**的消息，用于归集**已经生成的图表片段**；
    - 本函数回收**用户提问**与**工具调用入参**（不含工具输出正文），用于在
      「一个图表片段都没有」时仍然能触发**本地 K 线技术面图板**。

    ⚠️ 为什么必须分开（2026-11-02 实测）：fork 路径把
    ``tool_evidence=_chart_evidence_from_conversation(...)`` 传下去，上游一旦没产出片段
    （主力数据源从 mx_data_query 换成 iwc_query、或妙想免费额度耗尽），它就是空串，
    而 ``_maybe_build_tech_panels`` 拿到空证据直接 return [] —— **图表链路两层同时归零**，
    报告连一张本地 K 线图板都没有。工具入参里有 ``{"code": "512890"}``、用户提问里有
    「512890 + 515450」，这些才是标的的可靠来源。

    异常一律退化成空串（不阻断出报告）。
    """
    if not conversation:
        return ""
    parts: list[str] = []
    total = 0
    try:
        for msg in conversation:
            chunks: list[str] = []

            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, list):          # 多模态分段
                content = " ".join(
                    str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
                )
            if content:
                chunks.append(str(content))

            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls is None and isinstance(msg, dict):
                tool_calls = msg.get("tool_calls")
            for tc in tool_calls or []:
                try:
                    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                    if args is None:
                        continue
                    chunks.append(
                        args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                    )
                except Exception:
                    continue

            for chunk in chunks:
                if not chunk:
                    continue
                parts.append(chunk)
                total += len(chunk)
                if total >= _SYMBOL_HINT_MAX_CHARS:
                    return "\n".join(parts)[:_SYMBOL_HINT_MAX_CHARS]
    except Exception:
        logger.debug("report_builder: 从对话回收标的线索失败，退回空线索", exc_info=True)
    return "\n".join(parts)[:_SYMBOL_HINT_MAX_CHARS]


def _rough_token_count(text: str) -> int:
    """粗略估算中文报告 token 数，用于篇幅保护。"""
    if not text:
        return 0
    ascii_chars = 0
    non_ascii_chars = 0
    for ch in text:
        if ch.isspace():
            continue
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return non_ascii_chars + max(1, ascii_chars // 4)


def _visual_section_titles(template: dict) -> str:
    titles = []
    for section in template.get("sections", []):
        title = section.get("title", "")
        if title:
            titles.append(title)
    return "、".join(titles)


def _nonfork_meta(reason: str = "no_conversation") -> dict:
    """未走 fork 时的埋点（PRD-A11 FR-7）。

    三条报告调用在非 fork 路径下都**必须**打点，否则看板的 ``fork_ratio`` 会
    因为"缺字段"而虚高——看不出是"没 fork"还是"没记录"。
    """
    return {"fork_kind": "standalone", "fork_skip_reason": reason}


def _meta_with_node(metadata: dict | None, node: str) -> dict:
    """复制 metadata 并补 ``node``，使 trace 能按 label 归因（不修改调用方字典）。"""
    from utils.logger import with_trace_node
    return with_trace_node(metadata, node)


def build_shared_system(template: dict) -> str:
    """构建报告链路三条调用共用的 system 前缀（PRD-A9 §3.1）。

    三条调用 = report-select（证据下钻选择）/ report-fast（正文生成）/ dashboard（仪表盘）。
    它们在同一次 run 内连续执行，**共用同一份 system 即可命中 provider 的前缀缓存**。

    两条不变量（破坏任一条都会让缓存重新归零）：

    1. **只依赖 template**（模板 JSON），不掺入任何运行期数据 → 同一模板跨 run 字节稳定。
    2. **不读 section['_status']** → 同一模板在任何数据完整度下长度恒定。
       原实现遇到 ``_status == "skip"`` 会直接不渲染该 section，导致 system 长度随
       本轮工具命中情况漂移（实测同一模板两次 run 的前缀指纹不同、cached=0）。

    分支差异（原「路径 A 长文版式要求」vs「路径 B 简洁优先要求」）**下沉到 user[0]**，
    不留在 system——它们是任务指令而非平台约定，且 user[0] 紧随 system 之后，模型照样能读到。

    返回的字符串会被 ``llm_output["prefix_sha256"]`` 指纹化，单测可据此锁死「三调用一致」。
    """
    parts: list = []

    parts.append(template.get("role") or "你是一名资深证券分析师。")
    parts.append(
        "你正在为「选股雷达」（A 股智能分析系统）撰写分析报告。"
        "所有结论必须引用原始数据，禁止编造未在数据中出现的数字与事实。"
    )

    # ── 报告结构（全部 section，不因 _status 跳过；跳过信息由调用方写进 user[0]）──
    # 措辞沿用改动前路径 A 的原文（既有测试锁了「输出结构」），避免引入措辞层面的质量风险
    sections = template.get("sections", [])
    if sections:
        parts.append("\n## 输出结构（按此结构组织报告，每个板块有字数限制）")
        for section in sections:
            parts.append(f"\n### {section['title']}")
            bits = []
            if section.get("prompt"):
                bits.append(section["prompt"])
            if section.get("max_words"):
                bits.append(f"字数限制：{section['max_words']}字以内")
            if not section.get("required", False):
                # 措辞沿用改动前路径 A 原文（test_stock_analysis_backup_tools 锁了
                # 「禁止在本板块内重复罗列补齐方案」），只搬位置不改写
                bits.append(
                    "本板块必须输出；数据不足时本板块只写一句「数据缺失：<字段>」，"
                    "缺哪些字段与如何补齐统一放到报告末尾的「数据缺口声明」一次性说明，"
                    "禁止省略或整块留空，禁止在本板块内重复罗列补齐方案"
                )
            if bits:
                parts.append("要求：" + "；".join(bits))

    # ── 核心要求（两条路径合并，取并集；均为平台级硬约束）──
    parts.append(
        "\n## 核心要求\n"
        "1. 所有结论必须引用【原始工具数据 / 原始素材】中的具体数字和事实\n"
        "2. 禁止编造任何未在原始数据中提及的信息\n"
        "3. 必须同时列出正面和负面事实，不能只说一面\n"
        "4. 补充交叉验证分析（各维度是否共振/矛盾）\n"
        "5. 数据不足的板块必须写一句「数据缺失：<字段>」，并在报告末尾用「数据缺口声明」"
        "统一汇总（缺什么 + 怎么补），不能跳过也不能编造\n"
        "6. 术语区分（不得混淆）：**主动截断**（输出过长、数据已被工具取到但未全量载入上下文，"
        "原文标有「主动截断」「已省略中间 N 字符」）必须写成「主动截断（可补取）」，"
        "**不得**写成「数据缺失」；只有工具**明确返回错误/空结果/接口失败**才算「数据缺失」。"
        "也不得由某段被截断推断其它周期/字段缺失"
    )

    framework = template.get("analysis_framework")
    if framework:
        parts.append("\n## 分析判读框架（拿到数据后按此逻辑判断，报告结论必须体现这些判读逻辑）")
        if framework.get("summary"):
            parts.append(f"- {framework['summary']}")
        for rule in framework.get("rules", []):
            parts.append(f"- {rule}")

    cross_checks = template.get("cross_checks", [])
    if cross_checks:
        parts.append("\n## 交叉验证规则（综合研判板块必须遵守）")
        for check in cross_checks:
            parts.append(f"- {check.get('rule', '')}")

    report_fmt = template.get("report_format", {})
    if report_fmt:
        parts.append("\n## 格式规范")
        for rule in report_fmt.values():
            parts.append(f"- {rule}")

    qa_rules = template.get("qa_rules", [])
    if qa_rules:
        # 标题沿用改动前 _append_template_qa_rules 的原文（既有测试锁了它）
        parts.append("\n## 本模板质量要求（软约束）")
        parts.append("以下规则用于提高报告质量。若原始数据不足，请明确标注缺口，不要编造。")
        for rule in qa_rules:
            parts.append(f"- {rule}")

    parts.append("\n## 合规要求\n- 数据不足的板块必须标注\n- 风险提示必须前置，不隐瞒潜在风险")

    return "\n".join(parts)


def build_report_tail(template: dict) -> str:
    """报告**专属**任务指令 —— conversation fork 形态下追加在对话前缀之后。

    与 :func:`build_shared_system` 的区别：
      - ``build_shared_system`` = 平台角色 + 分析框架 + 报告专属指令，**整份**当 system，
        让 report-select / report-fast / dashboard 三条调用互为热前缀（PRD-A9 §3.1）。
      - ``build_report_tail`` **只取报告专属部分**，用于「复用 agent 已发热的对话前缀 +
        尾部追加任务指令」的 fork 形态（见 :func:`build_conversation_messages`）。

    为什么必须拆：agent 的 system 是 REACT_SYSTEM_PROMPT（1,544 字符），与
    build_shared_system（3,549 字符）是两份完全不同的文本。fork 时位置 0 必须是
    agent 那份才能命中缓存，因此报告专属指令只能搬到尾部。

    拆出去意味着 report-fast **不再**与 report-select / dashboard 共享前缀
    （那两条仍走 build_shared_system）。用 ~2.4k 尾部全价 token 换 ~20k 以上的
    热对话前缀，净收益为正。
    """
    parts: list = ["\n## 现在请撰写分析报告\n",
                   "以下面这段对话中你已获取的原始工具数据为准，撰写完整分析报告。"
                   "数据已在上下文中，无需重复列出工具原始输出，但所有结论必须引用其中的具体数字与事实。"]

    sections = template.get("sections", [])
    if sections:
        parts.append("\n## 输出结构（按此结构组织报告，每个板块有字数限制）")
        for section in sections:
            parts.append(f"\n### {section['title']}")
            bits = []
            if section.get("prompt"):
                bits.append(section["prompt"])
            if section.get("max_words"):
                bits.append(f"字数限制：{section['max_words']}字以内")
            if not section.get("required", False):
                bits.append(
                    "本板块必须输出；数据不足时本板块只写一句「数据缺失：<字段>」，"
                    "缺哪些字段与如何补齐统一放到报告末尾的「数据缺口声明」一次性说明，"
                    "禁止省略或整块留空，禁止在本板块内重复罗列补齐方案"
                )
            if bits:
                parts.append("要求：" + "；".join(bits))

    parts.append(
        "\n## 核心要求\n"
        "1. 所有结论必须引用【原始工具数据 / 原始素材】中的具体数字和事实\n"
        "2. 禁止编造任何未在原始数据中提及的信息\n"
        "3. 必须同时列出正面和负面事实，不能只说一面\n"
        "4. 补充交叉验证分析（各维度是否共振/矛盾）\n"
        "5. 数据不足的板块必须写一句「数据缺失：<字段>」，并在报告末尾用「数据缺口声明」"
        "统一汇总（缺什么 + 怎么补），不能跳过也不能编造\n"
        "6. 术语区分（不得混淆）：**主动截断**（输出过长、数据已被工具取到但未全量载入上下文，"
        "原文标有「主动截断」「已省略中间 N 字符」）必须写成「主动截断（可补取）」，"
        "**不得**写成「数据缺失」；只有工具**明确返回错误/空结果/接口失败**才算「数据缺失」。"
        "也不得由某段被截断推断其它周期/字段缺失"
    )

    cross_checks = template.get("cross_checks", [])
    if cross_checks:
        parts.append("\n## 交叉验证规则（综合研判板块必须遵守）")
        for check in cross_checks:
            parts.append(f"- {check.get('rule', '')}")

    report_fmt = template.get("report_format", {})
    if report_fmt:
        parts.append("\n## 格式规范")
        for rule in report_fmt.values():
            parts.append(f"- {rule}")

    qa_rules = template.get("qa_rules", [])
    if qa_rules:
        parts.append("\n## 本模板质量要求（软约束）")
        parts.append("以下规则用于提高报告质量。若原始数据不足，请明确标注缺口，不要编造。")
        for rule in qa_rules:
            parts.append(f"- {rule}")

    parts.append("\n## 合规要求\n- 数据不足的板块必须标注\n- 风险提示必须前置，不隐瞒潜在风险")

    return "\n".join(parts)


def _skipped_sections_note(template: dict) -> str:
    """把本轮被跳过的板块写成一句 user 侧说明。

    原实现直接在 system 里不渲染 `_status == "skip"` 的 section，
    既让 system 长度随数据完整度漂移（破缓存），也让模型「看不到该板块存在」
    而在正文里凭空补写。改为：system 始终渲染全部板块（结构稳定），
    再由 user[0] 明确告知本轮哪些板块不用写。
    """
    skipped = [s["title"] for s in template.get("sections", [])
               if s.get("_status") == "skip"]
    if not skipped:
        return ""
    return ("\n\n## 本轮无需输出的板块\n"
            "以下板块本轮数据缺失，**直接不写**（不要留空、不要用「数据缺失」占位）：\n"
            + "\n".join(f"- {t}" for t in skipped))


def _length_profile_note(template: dict) -> str:
    """篇幅口径提示（原先按分支写在 system 里，现下沉到 user[0]）。

    **标题与措辞沿用改动前原文**（既有测试分别锁了「长文与版式要求」与「简洁优先」），
    只是换了所在位置，避免引入措辞层面的质量风险。

    - ``min_report_tokens > 0`` 的模板：给足篇幅但禁止注水
    - ``min_report_tokens == 0`` 的轻量模板：反向要求，简洁优先
    """
    min_tok = _min_report_tokens(template)
    max_w = _max_report_words(template)
    if min_tok > 0:
        head = "## 长文与版式要求"
        line = (f"- 全文目标 {min_tok} token 级别即可，不追求越长越好，禁止为凑篇幅注水。\n"
                "- 优先把篇幅给「核心结论」「行动方案」：结论要方向+价位，行动要触发条件+失效条件。\n"
                "- 表格/矩阵/条件树只在能承载判断时使用，禁止为版式新增空洞表格或重复风险提示。\n"
                "- 关键结论必须结构化，禁止用大段纯文字硬堆。")
        if max_w:
            line += f"\n- 全文不超过 {max_w} 字，超过即为注水。"
    else:
        head = "## 篇幅要求（重要）"
        line = ("- **简洁优先**：只输出用户明确要求的内容。能用一行表格或一句话说清，就绝不分节展开。\n"
                "- **禁止补充用户没要的板块**：分析、建议、候选排序、仓位、交易纪律、投资视角、"
                "风险矩阵、小白版翻译等一律不写。\n"
                "- 必须标注数据口径（具体交易日 / 数据源），取不到就写「数据缺失：<字段>」。")
    return head + "\n" + line


def build_conversation_messages(template: dict, conversation, *,
                                conversation_tools=None,
                                tail_builder=None) -> list | None:
    """conversation fork：复用 agent 已发热的对话前缀，任务指令**尾部追加**。

    形态::

        [tools 数组 · 与 agent 逐字节相同 · 位置 0]
        [agent 的 system · 与 agent 相同]
        [...完整对话历史...]
        [尾部: 本次任务指令]

    **能命中缓存的原因**：agent 最后一次 LLM 调用发出的 payload 就是上面除尾部之外的
    全部内容，且刚刚被 provider 缓存过。本次调用只需为尾部付全价。

    **必须绑定同一份 tools 数组**（``conversation_tools``）：位置 0 变了，
    整条前缀立即归零。full 工具面（``TOOL_SURFACE_MODE=full``）是它的必要前置——
    strict 档 tools 随技能批变动，绑定也无从对齐。

    :param tail_builder: 可选，``() -> str``。默认用 :func:`build_report_tail`。
        PRD-A11 FR-3：下钻选择 / 扩写复用同一个前缀族，各自换自己的尾部指令。

    返回 ``None`` 表示不适用（无对话/ 无工具），调用方回落到证据注入路径。
    这是**能力判定**而非开关：plan / pdor / agent_group 的报告输入是跨 step 聚合，
    单条对话里本就不存在这批工具结果，强行 fork 反而更差。
    """
    if not conversation or not conversation_tools:
        return None

    tail = tail_builder() if tail_builder else build_report_tail(template)
    if not tail:
        return None
    # ⚠️ 用 user 而非 system 承载尾部指令：agent 最后一次调用的消息序列必然以
    # tool 结果结尾（拿到数据后循环结束），而「tool 结果紧跟 system」在部分
    # provider 上是非法序列；user 紧跟 tool 则到处都合法。
    # 尾部的角色对缓存没有影响 —— 它在最末尾，本来就是全价新 token。
    msgs = list(conversation)
    return [*msgs, ("user", tail)]


def _expand_report_if_short(content: str, template: dict, user_input: str,
                            llm, log, budget=None, metadata=None, run_config=None,
                            *, conversation=None, conversation_tools=None) -> str:
    """报告过短时扩写一次，补足深度和图表化结构。"""
    from utils.llm_factory import tracked_invoke

    min_tokens = _min_report_tokens(template)
    current_tokens = _rough_token_count(content)
    if min_tokens <= 0 or current_tokens >= min_tokens:
        return content

    log.info("A", f"报告篇幅不足，触发扩写: {current_tokens}/{min_tokens} token")
    sections = _visual_section_titles(template)
    tail = "\n".join([
        "## 本次任务\n你是资深证券分析师和中文报告编辑。只能基于原报告已有事实扩写，禁止编造新数据。",
        f"用户问题：{user_input}",
        f"当前报告约 {current_tokens} token，低于最低目标 {min_tokens} token。",
        "请在不改变原结论、不编造新数据的前提下扩写为完整长报告。",
        "扩写要求：",
        f"- 全文不少于 {min_tokens} token 级别。",
        "- 保留并强化这些板块：" + sections,
        "- 只在确有对比维度时补 Markdown 表格；**不得为凑篇幅生成 Mermaid / 条件树 / 关系图 / 操作树**（除非原报告 1.0 里已有）。",
        "- 数据不足处写「数据缺失」并解释对结论的影响。",
        # PRD-A11 FR-3 / Q6：走 fork 后扩写能看见工具结果 → 必须收紧"不许编造"的边界，
        # 否则它会拿前缀里的原始数字"补写"出报告 1.0 里没有的结论。
        "- **只允许引用上面对话里已出现的数字**；报告 1.0 里没有的数字不得补写。"
        "确实需要新数据时，输出 {\"need_tools\": [...]} 说明要补什么，而不是编造。",
        "- 不要输出改写说明，直接输出最终报告。",
        "\n原报告：",
        content,
    ])

    # PRD-A11 FR-3：扩写也走 fork —— 原实现只拿到「报告 1.0 全文」，
    # 看不见任何工具结果与模板契约，这正是「基于 1.0 写废话」的机制性成因。
    fork_meta: dict = {}
    messages = None
    if conversation and conversation_tools:
        messages = build_conversation_messages(
            template, conversation, conversation_tools=conversation_tools,
            tail_builder=lambda: tail)
        if messages is not None:
            from agents.shared.llm_fork import fork_metadata
            from agents.shared.prompt_prefix import make_fork_prefix
            fork_meta = fork_metadata(
                make_fork_prefix(conversation, conversation_tools, source="report"),
                (messages, conversation_tools))
            # ⚠️ fork 必须把**同一份** tools 数组绑回实例（位置 0 是前缀的一部分）。
            # 实测（2026-10-17，同一 run 逐调用比对 trace）：同一个消息前缀，
            # report-fast 绑了 52 个工具 / 12,813 字符 → cached 31,872（93%）；
            # report-expand 没绑 → cached 2,048（7%，正好是 provider 最小块）——
            # 少了 tools 就是前缀错位，整条前缀归零。
            # 绑定方式与 report-fast 同款（失败回落无工具，仍可用对话前缀）。
            # ⚠️ 只在 fork 分支内绑：下面 `if messages is None` 的兜底路径是 [system, user] 两条新消息，
            # 没有任何前缀可对齐，多带 8k tools 纯亏。
            try:
                llm = llm.bind_tools(conversation_tools)
            except Exception as e:
                log.warning("A", f"扩写 fork 绑定工具失败，改用无工具调用: {e}")
    if messages is None:
        messages = [
            ("system", "你是资深证券分析师和中文报告编辑。只能基于原报告已有事实扩写，禁止编造新数据。"),
            ("user", tail),
        ]
        fork_meta = _nonfork_meta("no_conversation")
    resp = tracked_invoke(llm, messages, log, label="report-expand", budget=budget,
                          metadata={**_meta_with_node(metadata, "report-expand"), **fork_meta},
                          run_config=run_config)
    expanded = resp.content if hasattr(resp, "content") else str(resp)
    return expanded or content


def _format_tool_calls(tool_calls: list) -> str:
    """将工具调用历史格式化为文本，直接传给 LLM"""
    if not tool_calls:
        return ""
    lines = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name", tc.get("tool_name", ""))
            args = tc.get("args", tc.get("tool_input", ""))
            output = tc.get("tool_output", "") or tc.get("output", "")
        else:
            name = getattr(tc, "tool_name", "") or getattr(tc, "name", "")
            args = getattr(tc, "tool_input", "") or getattr(tc, "args", "")
            output = getattr(tc, "tool_output", "") or getattr(tc, "output", "")
        if not output:
            continue
        lines.append(f"### 工具调用: {name}")
        if args:
            lines.append(f"输入: {args}")
        lines.append(f"输出:\n{output}")
        lines.append("")
    return "\n".join(lines)


# ── 索引 → 序号下钻（报告证据聚焦） ────────────────────────────────────
# 默认开启：全量工具证据改为「编号索引 → LLM 按序号选 → 只载入选中全文」。
# 核心目的不是省 token，而是让报告 LLM 的注意力集中到与问题相关的原始数据上；
# 未入选的调用以「已取到但未载入（可补取）」清单显式告知，绝不允许被写成「数据缺失」。

def _tool_status(output) -> str:
    """工具调用状态：ok / failed / empty（「数据缺失」只允许来自 failed/empty）。

    实现已抽到 `evidence_view.tool_output_status`，与仪表盘共用同一事实源。
    """
    return tool_output_status(output)


def _parse_need_indices(raw_json, max_index: int) -> list:
    """解析 {"need_tools": [1,3,5]} → 有效序号（1-based，去重保序）。"""
    if not isinstance(raw_json, dict):
        return []
    nums = raw_json.get("need_tools")
    if nums is None:
        nums = raw_json.get("need_indices")
    if isinstance(nums, (int, float, str)):
        nums = [nums]
    if not isinstance(nums, list):
        return []
    seen, out = set(), []
    for n in nums:
        try:
            idx = int(float(str(n).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= max_index and idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _build_selection_prompt(items: list, user_input: str, raw_result: str,
                           template: dict | None = None, *,
                           conversation=None, conversation_tools=None) -> list:
    """阶段一 prompt：用户问题 + 分析草稿（全文，不截断）+ 编号工具索引。

    **两条路径**（PRD-A11 FR-3）：

    - **fork 档**（有 ``conversation`` + ``conversation_tools``）：复用 agent 已热的
      完整对话前缀，尾部只追加筛选指令。与 report-fast / dashboard 属**同一前缀族**，
      于是三条调用共享整条前缀（实测：原路径只有 2,009 tok 共享，正文那条前缀达 20~45K）。
    - **回落档**（无对话）：保留 PRD-A9 §3.1 的 ``build_shared_system`` system —— 
      多步模式（plan/pdor/group）没有单条对话可复用，这份 system 仍是它们之间唯一的
      共享手段，不能丢。

    ``template`` 为 None 时（仅单测/调试直接调用）退回空模板——此时 system 会很短，
    属于降级路径，生产链路一定会传进来。
    """
    lines = []
    for i, it in enumerate(items, 1):
        text = _text(it.get("output"))
        args = (it.get("args") or "").replace("\n", " ")[:120]
        status = _tool_status(it.get("output"))
        tag = {"failed": "（失败）", "empty": "（空结果）", "ok": ""}[status]
        preview = text.strip()[:120].replace("\n", " ")
        lines.append(
            f"{i}. {it['name']} | 参数:{args or '—'} | 长度:{len(text)}{tag} | 预览:{preview or '（空）'}"
        )

    user = (
        "## 本次任务\n"
        "你是报告撰写前的**证据筛选器**，不是报告撰写者。输入包含：用户问题、"
        "agent 已写好的分析草稿、以及本次运行收集到的全部工具结果索引（每条带序号）。\n"
        "任务：挑出「为写出高质量最终报告，需要载入全文」的工具结果序号。\n"
        "原则：\n"
        "1. 按「报告会直接引用」的标准选择：与用户问题、分析草稿结论直接相关的原始数据必选；"
        "草稿未覆盖但问题涉及维度的条目也要选。\n"
        "2. 低信息量条目（长度很短、只有报错或元信息）不必选；"
        "同类重复的调用只保留信息最完整的一条（如多次查询同一主题且后一条已包含前一条内容）。\n"
        "3. 已标注失败/空结果的条目不选，它们不提供数据。\n"
        "4. 未入选不等于丢弃：下游会把未入选调用以「未载入（可补取）」显式标注，"
        "不会造成数据缺失误报；因此请按相关性做真实筛选，不要为保险起见全选。\n"
        "5. 数量不设上限：需要几个选几个。\n"
        '6. 只输出严格 JSON，不要任何其他文字：{"need_tools": [序号, ...]}'
        '\n\n## 用户问题\n' + user_input
        + '\n\n## 分析草稿（全文）\n'
        + (raw_result or '（本轮没有中间草稿，请仅凭问题与索引选择）')
        + "\n\n## 工具结果索引\n" + "\n".join(lines)
        + '\n\n请输出 JSON：{"need_tools": [序号, ...]}'
    )
    # fork 档：同一条已热对话前缀 + 尾部筛选指令（与 report-fast / dashboard 同族）
    if conversation and conversation_tools:
        forked = build_conversation_messages(
            template, conversation, conversation_tools=conversation_tools,
            tail_builder=lambda: user)
        if forked is not None:
            return forked
    # 回落：筛选器需要 template 参数才能与 report-fast 共用 system，
    # 生产链路由 _build_drilldown_evidence 透传。
    return [("system", build_shared_system(template or {})), ("user", user)]


def _format_remaining_calls(items: list, selected_indices: set) -> str:
    """未入选调用的状态清单：未载入（可补取）+ 失败/空结果（缺失唯一来源）。"""
    failed, unloaded = [], []
    for i, it in enumerate(items, 1):
        if i in selected_indices:
            continue
        args = (it.get("args") or "").replace("\n", " ")[:100]
        status = _tool_status(it.get("output"))
        if status == "failed":
            failed.append(f"- #{i} {it['name']} | 参数:{args or '—'} → 调用失败，无数据")
        elif status == "empty":
            failed.append(f"- #{i} {it['name']} | 参数:{args or '—'} → 空结果，无数据")
        else:
            unloaded.append(
                f"- #{i} {it['name']} | 参数:{args or '—'} | 长度:{len(_text(it.get('output')))}"
            )
    parts = []
    if unloaded:
        parts.append(
            "## 已取到但未载入全文的调用（可补取）\n"
            + "\n".join(unloaded)
            + "\n以上结果已由工具取到，只是为聚焦注意力未载入全文："
            "**只能表述为「未载入（可补取）」，不得写成「数据缺失」**。"
        )
    if failed:
        parts.append(
            "## 失败/空结果调用（「数据缺失」的唯一直属来源）\n"
            + "\n".join(failed)
            + "\n只有以上调用才允许在报告中写「数据缺失」。"
        )
    return "\n\n".join(parts)


async def _select_tool_indices(items: list, user_input: str, raw_result: str,
                               budget, log, metadata, run_config,
                               template: dict | None = None, *,
                               conversation=None, conversation_tools=None) -> Optional[list]:
    """阶段一：LLM 按序号选择。返回 1-based 序号列表；None = 放弃下钻（退回全量）。

    ``template`` 透传给 _build_selection_prompt，让 report-select 的 system
    与 report-fast 字节一致（PRD-A9 §3.1）。

    PRD-A11 FR-3：``conversation`` 非空时改走 fork —— 与 report-fast / dashboard 属
    同一前缀族。实测原路径（build_shared_system）只有 2,009 tok 与正文共享，
    而正文那条前缀已 20~45K；且异族调用会触发 provider 侧缓存驱逐
    （PRD-A11 §2.12：两次 report-fast 命中 0% 都发生在异族调用插入之后）。
    """
    total_chars = sum(len(_text(it.get("output"))) for it in items)
    min_tools = int(getattr(Config, "REPORT_DRILLDOWN_MIN_TOOLS", 8) or 8)
    min_chars = int(getattr(Config, "REPORT_DRILLDOWN_MIN_CHARS", 12000) or 12000)
    if len(items) < min_tools and total_chars < min_chars:
        return None  # 证据太小，下钻不划算
    if budget is not None:
        try:
            remaining = budget.limits.max_llm_calls_per_query - budget.get_calls()
            if remaining <= 1:
                log.info("A", "报告下钻跳过：预算紧张")
                return None
        except Exception:
            pass

    from utils.llm_factory import get_report_llm, llm_json_with_retry
    messages = _build_selection_prompt(items, user_input, raw_result, template,
                                       conversation=conversation,
                                       conversation_tools=conversation_tools)
    fork_meta: dict = _nonfork_meta("no_conversation")
    if conversation and conversation_tools and len(messages) > 1:
        from agents.shared.llm_fork import fork_metadata
        from agents.shared.prompt_prefix import make_fork_prefix
        fork_meta = fork_metadata(
            make_fork_prefix(conversation, conversation_tools, source="report"),
            (messages, conversation_tools))
    try:
        raw = llm_json_with_retry(
            get_report_llm(), messages, log, label="report-select",
            max_retries=1, budget=budget,
            metadata={**_meta_with_node(metadata, "report-select"), **fork_meta},
            run_config=run_config,
        )
    except BudgetExceeded:
        raise
    except Exception as e:
        log.info("A", f"报告下钻选择失败，退回全量: {e}")
        return None
    indices = _parse_need_indices(raw, len(items))
    if not indices:
        log.info("A", f"报告下钻未选中任何调用，退回全量（raw={str(raw)[:120]}）")
        return None
    return indices


async def _build_drilldown_evidence(items: list, user_input: str, raw_result: str,
                                    budget, log, metadata, run_config,
                                    template: dict | None = None, *,
                                    conversation=None,
                                    conversation_tools=None) -> str:
    """下钻证据装配：选中全文 + 未入选状态清单；任何异常 → 全量（零回归）。

    ``template`` 透传给 report-select，使其 system 与 report-fast 完全一致（PRD-A9 §3.1）。
    ``conversation`` 透传给 report-select，使其走 fork（PRD-A11 FR-3）。
    """
    if not getattr(Config, "REPORT_DRILLDOWN_ENABLED", True) or not items:
        return _format_tool_calls(items)
    selected = await _select_tool_indices(
        items, user_input, raw_result, budget, log, metadata, run_config,
        template=template, conversation=conversation,
        conversation_tools=conversation_tools)
    if not selected:
        return _format_tool_calls(items)
    sel_set = set(selected)
    picked = [it for i, it in enumerate(items, 1) if i in sel_set]
    body = _format_tool_calls(picked)
    remaining = _format_remaining_calls(items, sel_set)
    if remaining:
        body = (body + "\n\n" + remaining).strip()
    total_chars = sum(len(_text(it.get("output"))) for it in items)
    log.info("A", f"报告下钻: 选中 {len(picked)}/{len(items)} 个调用，"
                  f"证据 {total_chars}→{len(body)} 字符（序号 {selected}）")
    return body


def _append_template_qa_rules(parts: list, template: dict):
    """将当前模板的质量要求作为软约束注入最终报告 prompt。"""
    qa_rules = template.get("qa_rules", [])
    if not qa_rules:
        return

    parts.append("\n## 本模板质量要求（软约束）")
    parts.append("以下规则用于提高报告质量。若原始数据不足，请明确标注缺口，不要编造。")
    for rule in qa_rules:
        parts.append(f"- {rule}")


def _package_report(content: str, template: dict, has_rich_raw: bool, log=None) -> dict:
    """把 LLM 正文封装成 ReportDocument（fork 与证据注入两条路径共用）。

    抽出来是为了让 fork 分支不重复这段模式 A/B 分派逻辑——两条路径的**输入构造**
    不同，但**输出契约**必须逐字节一致，否则 fork 开着关着报告结构会变。
    """
    if has_rich_raw:
        return {
            "title": template.get("name", "分析报告"),
            "summary": _extract_first_section(content),
            "sections": [ReportSection(
                id="full_report", title="分析报告",
                content=content, status="ok",
            )],
            "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
        }
    sections = _parse_report_sections(content, template)
    # 兜底：解析出 0 个板块但 LLM 输出内容充足时，按完整报告处理，
    # 避免 sections 为空导致 format_report 只输出标题+免责声明、正文全部丢失。
    if not sections and content and len(content) > 200:
        # ⚠️ 必须用调用方传入的 RadarLogger（log），不能用模块级 logging logger：
        # 后者会把 "A" 当成 % 格式化参数（"not all arguments converted"），
        # pytest 的 logging 插件在格式化该记录时抛错并冒泡，改变返回路径。
        (log or logger).warning(
            f"报告板块解析失败(0/{len(template.get('sections', []))})，回退为完整报告输出")
        return {
            "title": template.get("name", "分析报告"),
            "summary": _extract_first_section(content),
            "sections": [ReportSection(
                id="full_report", title="分析报告",
                content=content, status="ok",
            )],
            "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
        }
    return {
        "title": template.get("name", "分析报告"),
        "summary": _extract_summary(sections),
        "sections": sections,
        "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
    }


async def build(template: dict, slot_results: dict, user_input: str,
                budget=None, logger_obj=None, raw_result: str = "",
                tool_calls: list = None, selected_skills: list = None,
                metadata=None, run_config=None, step_results: list = None,
                raw_result_is_seed: bool = False,
                conversation=None, conversation_tools=None,
                fork_sink: dict | None = None) -> dict:
    """
    快速模式报告合成。

    默认 2 次 LLM 调用：序号下钻选择（report-select）+ 报告生成（report-fast）；
    证据过小 / 选择失败 / 下钻关闭时退回 1 次全量调用（与旧行为一致，零回归）。

    raw_result: Agent 原始输出（下钻选择的种子，全文使用，不截断）
    tool_calls: 工具调用历史，直接传入 prompt 作为原始证据
    raw_result_is_seed: raw_result 只是「下钻种子」而非待注入素材
        —— 仅 agent_group 使用（它的原始数据走条目/下钻通道，raw_result 只是选择依据）。
        ⚠️ 默认 False = 保持既有行为：React / Unified / Plan / PDOR 模式
        （它们的 raw_result 是报告草稿，必须注入）。改这个默认值会同时影响 5 个模式。

    conversation / conversation_tools（PRD-A9 §14，fork 形态）：
        传入 agent 的完整对话与其绑定的同一份工具数组后，report-fast 改为
        「复用该已热前缀 + 尾部追加报告指令」，并**跳过 report-select**（工具结果
        已在对话里，无需再选一次载什么）。任一为空则自动回落证据注入路径 ——
        这是能力判定不是开关，plan/pdor/group 的跨 step 聚合天然不适用。

    返回 ReportDocument dict: {"title", "summary", "sections": [ReportSection], "disclaimer"}
    """
    log = logger_obj or logger
    # 预算不足时仅警告，不降级到规则化报告
    if budget:
        status = budget.get_status()
        if status.get("calls_percent", 0) > 85:
            log.warning(f"预算紧张 ({status.get('calls_percent', 0):.0f}%)，报告生成可能不完整")

    try:
        from utils.llm_factory import get_report_llm, tracked_invoke

        has_rich_raw = raw_result and len(raw_result) > 500

        # ── fork 形态：复用 agent 已热前缀 ────────────────────────────
        fork_messages = build_conversation_messages(
            template, conversation, conversation_tools=conversation_tools)
        if fork_messages is not None:
            log.info("A", f"conversation fork: 复用 {len(fork_messages) - 1} 条对话前缀，"
                          f"绑定 {len(conversation_tools)} 个工具，跳过 report-select")
            from agents.shared.llm_fork import fork_metadata
            from agents.shared.prompt_prefix import make_fork_prefix
            # PRD-A11 FR-7：把「这次 fork 共享了多少前缀」写进 trace。之后任何人问
            # 「report-fast 到底 fork 了没有」，都能从 steps.extra 直接读，不用猜。
            fork_meta = fork_metadata(
                make_fork_prefix(conversation, conversation_tools, source="report"),
                (fork_messages, conversation_tools))
            llm = get_report_llm()
            try:
                llm = llm.bind_tools(conversation_tools)
            except Exception as e:      # 绑定失败则退回无工具（仍可用对话前缀）
                log.warning("A", f"fork 绑定工具失败，改用无工具调用: {e}")
            resp = tracked_invoke(
                llm, fork_messages, log, label="report-fast", budget=budget,
                metadata={**_meta_with_node(metadata, "report-fast"), **fork_meta},
                run_config=run_config)
            content = resp.content if hasattr(resp, "content") else str(resp)
            report_llm = get_report_llm()
            content = _expand_report_if_short(
                content, template, user_input, report_llm, log, budget=budget,
                metadata=_meta_with_node(metadata, "report-expand"),
                run_config=run_config,
                conversation=conversation, conversation_tools=conversation_tools)
            # fork 路径没有单独的 tool_evidence，图表标记要从对话的工具消息里回收，
            # 否则报告正文永远不会出现 @@CHART（图表全丢）。
            # code_hint 是「没有任何图表片段时」的独立兜底：从用户提问与工具入参里
            # 捞标的代码，让本地 K 线技术面图板照样能出图（2026-11-02 修复）。
            content = _merge_chart_markers(
                content,
                tool_evidence=_chart_evidence_from_conversation(conversation),
                code_hint=_conversation_symbol_hints(conversation),
                template=template,
            )
            return _package_report(content, template, has_rich_raw, log)

        # 格式化原始工具调用结果（默认走「索引→序号下钻」聚焦证据；失败自动退回全量）
        tool_evidence = ""
        if tool_calls:
            raw_sizes = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    sz = len(tc.get("tool_output", "") or tc.get("output", "") or "")
                else:
                    sz = len(getattr(tc, "tool_output", "") or "")
                raw_sizes.append(sz)
            log.info("A", f"工具输出大小: {raw_sizes}, 总计={sum(raw_sizes)}字符")
            items = _tool_items(tool_calls)
            tool_evidence = await _build_drilldown_evidence(
                items, user_input, raw_result, budget, log, metadata, run_config,
                template=template, conversation=conversation,
                conversation_tools=conversation_tools)
            if tool_evidence:
                log.info("A", f"工具证据（格式化后）: {len(tool_evidence)} 字符")

        messages = _build_prompt(
            template, slot_results, user_input,
            raw_result=raw_result,
            tool_evidence=tool_evidence,
            step_results=step_results,
            raw_result_is_seed=raw_result_is_seed,
        )
        # PRD-A11：把本次 report-fast **实际发出的 payload** 冻结下来，随结果回到
        # report_enhance_node → 写进 state，供**下一个节点（仪表盘）复用同一前缀**。
        # 「报告在前、仪表盘在后」且只隔约 1 秒 —— 实测立即复用可命中（正常网络约 3/4）。
        # 这是 plan/pdor/group 这类「跨 step 聚合」模式唯一能拿到的同 run 预热源。
        if fork_sink is not None:
            fork_sink["messages"] = list(messages)
            fork_sink["tools"] = []
        resp = tracked_invoke(get_report_llm(), messages, log, label="report-fast", budget=budget,
                              metadata={**_meta_with_node(metadata, "report-fast"),
                                        **_nonfork_meta("no_conversation")}, run_config=run_config)
        content = resp.content if hasattr(resp, "content") else str(resp)
        report_llm = get_report_llm()
        content = _expand_report_if_short(
            content, template, user_input, report_llm, log, budget=budget,
            metadata=_meta_with_node(metadata, "report-expand"), run_config=run_config,
            conversation=conversation, conversation_tools=conversation_tools
        )

        # 图表标记归集：从工具证据中收集萃取生成的 @@CHART:uuid@@ 标记。
        # 1) 按 (stock_code, chart_type) 去重——同一股票同类型图表只保留最新一张
        #    （此前只按 ID 去重，而每次 mx_data_query 都生成新 UUID，
        #     导致报告中出现 2~3 张同 code 的 MACD）。
        # 2) 对主股票尝试注入一张「技术面图板」增强卡（本地 K 线 + 全指标 +
        #    金叉死叉/突破信号，前端 Tab 切换）。有枣打两棍子：失败静默跳过。
        # 图表标记归集：去重 + 清理 LLM 复述残留 + 注入技术面图板（统一入口）
        # template 决定标的类型口径：市场日报类（asset_type=market +
        # intent_type=daily_summary）里的 000001 按上证指数解释，而非平安银行
        content = _merge_chart_markers(content, tool_evidence=tool_evidence,
                                       template=template)

        if has_rich_raw:
            # 模式 A：LLM 输出是结构化增强后的完整报告，直接使用
            return _package_report(content, template, has_rich_raw, log)
        # 模式 B：按模板板块解析（与 fork 路径共用同一封装，输出契约逐字节一致）
        return _package_report(content, template, has_rich_raw, log)
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning(f"LLM 报告合成失败，回退到规则化报告: {e}")
        return _rule_based_report(template, slot_results, user_input)


def _build_prompt(template: dict, slot_results: dict, user_input: str,
                  raw_result: str = "", tool_evidence: str = "",
                  step_results: list = None, raw_result_is_seed: bool = False) -> list:
    """构建报告合成 prompt（PRD-A9 §3.1：system 前缀与运行期数据彻底解耦）。

    核心原则：原始工具数据 > 第一次总结 > 模板结构
    LLM 能自己从原始数据中提取关键信息，不需要规则预处理

    **布局不变量**（改动前务必读 CLAUDE 式注释里的实测结论）：
      ``[system = build_shared_system(template)]`` ← 只依赖模板，跨 run 字节稳定
      ``[user[0]]``                          ← 任务指令 + 篇幅口径 + 本轮跳过板块
      ``[user[1:]]``                         ← 原始数据（每次不同，不参与缓存）
    report-select / report-fast / dashboard 三条调用共享同一份 system，
    同一次 run 内连续执行即可命中 provider 前缀缓存。
    """
    horizon = template.get("time_horizon", {}).get("default", "short")
    horizon_config = template.get("time_horizon", {}).get("presets", {}).get(horizon, {})

    # 判断模式：raw_result 足够丰富时，以它为主；否则以插槽数据为主
    has_rich_raw = raw_result and len(raw_result) > 500

    # ── user[0]：本次任务的专属指令（不进 system，避免前缀随分支漂移）──
    head = [_length_profile_note(template)]

    if has_rich_raw:
        if step_results:
            head.append(
                "## 本次任务\n"
                "以下是多个步骤的分析结果，每个步骤侧重不同维度。"
                "请综合所有步骤的信息，按上述标准结构重新组织为一份完整报告。\n"
                "**禁止直接复制任何一个步骤的报告，必须合并所有步骤的增量信息。**"
            )
        else:
            head.append(
                "## 本次任务\n"
                "请基于原始工具数据和已有分析报告，按上述标准结构重新组织为完整报告。"
            )
        head.append(
            "注意：本条系统消息里的「报告结构」是本模板的**完整**结构，"
            "即使本轮某板块无数据也要保留其位置（按核心要求第 5 条写缺失标注）。"
        )
    else:
        head.append("## 本次任务\n请按照上述模板结构生成分析报告。")

    head.append(
        f"## 时间框架\n当前分析框架：{horizon_config.get('label', '短线')}"
        f"（{horizon_config.get('description', '')}）"
    )

    skipped_note = _skipped_sections_note(template)
    messages = [
        ("system", build_shared_system(template)),
        ("user", "\n\n".join(head) + skipped_note),
    ]

    if has_rich_raw:
        # user[1]: 原始工具数据（独立消息，便于下钻聚焦）
        if tool_evidence:
            messages.append(("user", f"## 原始工具数据（所有结论必须基于此）\n{tool_evidence}"))

        # PDOR 模式：每个 step 结果作为独立 AI message
        if step_results:
            for sr in step_results:
                messages.append(("ai", (
                    f"### Step {sr.get('step', '?')} [{sr.get('skill', '')}]: "
                    f"{sr.get('purpose', '')}\n{sr.get('result', '')}"
                )))
        elif not (raw_result_is_seed and tool_evidence):
            # 注入 raw_result 作为原始素材。
            # 唯一的例外：agent_group 传 raw_result_is_seed=True —— 它的 raw_result
            # 只是下钻选择的种子，原始数据已由 tool_evidence（条目索引 → 按需载入）承载，
            # 再注入会重复。**其余模式（React/Unified/Plan/PDOR）默认 False，行为与改动前完全一致**
            # —— 注意 React/Unified 的 step_results 为空、tool_calls 非空，raw_result 是它们的
            # 报告草稿（state["final_result"] / response），不能被冲掉。
            # 2026-10-01 事故：原实现 `if step_results / else raw_result` 使 group 恒拿不到底版。
            messages.append(("user", f"## 原始素材（优先引用，所有结论必须基于此）\n{raw_result}"))

        extra_slots = [f"- {k}: {v.interpretation}" for k, v in slot_results.items()
                       if isinstance(v, SlotResult) and v.interpretation]
        tail = []
        if extra_slots:
            tail.append("## 补充结构化数据\n" + "\n".join(extra_slots))
        tail.append(f"## 用户问题\n{user_input}")
        messages.append(("user", "\n\n".join(tail)))
        return messages

    # ── 插槽模式：各板块数据放 user 侧（slot 状态本就在 user，原实现已如此）──
    parts = ["## 分析任务\n请按照上述模板结构生成分析报告。"]
    if tool_evidence:
        parts.append(f"## 原始工具数据\n{tool_evidence}")

    for section in template.get("sections", []):
        status = section.get("_status", "ok")
        if status == "skip":
            continue
        section_lines = []
        if status in ("degraded", "partial"):
            section_lines.append(f"注意：{section.get('_note', '')}")
        for slot_name in section.get("data_slots", []):
            sr = slot_results.get(slot_name)
            if not sr or not isinstance(sr, SlotResult):
                continue
            if sr.method in ("json", "table", "regex", "derived"):
                section_lines.append(f"- {slot_name}: {sr.interpretation}")
            else:
                section_lines.append(f"- {slot_name}: {json.dumps({'value': sr.value, 'status': sr.status, 'interpretation': sr.interpretation}, ensure_ascii=False)}")
        if section_lines:
            parts.append(f"\n### {section['title']} 数据\n" + "\n".join(section_lines))

    parts.append(f"\n## 用户问题\n{user_input}")
    return [
        ("system", build_shared_system(template)),
        ("user", "\n\n".join(head) + skipped_note + "\n\n" + "\n\n".join(parts)),
    ]

def _normalize_heading(line: str) -> str:
    """归一化 markdown 标题行，去掉中文/阿拉伯序号前缀。

    LLM 常在标题前加 "一、二、" 或 "1. 2." 等序号，
    导致 _parse_report_sections 的 startswith("## {title}") 匹配失败。
    例： "## 一、核心结论" -> "## 核心结论"
         "### 3.技术面分析" -> "### 技术面分析"
    """
    # 匹配 ## / ### 开头后的序号前缀：中文数字(一、壹)或阿拉伯数字+点/顿号
    m = re.match(r'^(#{1,6})\s*([一二三四五六七八九十百壹贰叁肆伍陆柒捌玖拾]+[、.]|\d+[.\)、])\s*', line.strip())
    if m:
        return f"{m.group(1)} {line.strip()[m.end():].lstrip()}"
    return line.strip()


def _parse_report_sections(content: str, template: dict) -> list:
    """从 LLM 输出中解析报告板块。"""
    sections = []
    current_section = None
    current_lines = []

    for line in content.split("\n"):
        matched = False
        normalized = _normalize_heading(line)
        for section_def in template.get("sections", []):
            title = section_def["title"]
            if (normalized.startswith(f"## {title}") or
                normalized.startswith(f"### {title}") or
                normalized.startswith(f"【{title}】") or
                normalized == title or
                normalized.startswith(f"# {title}")):
                if current_section:
                    sections.append(ReportSection(
                        id=current_section["id"],
                        title=current_section["title"],
                        content="\n".join(current_lines).strip(),
                        status=current_section.get("_status", "ok"),
                    ))
                current_section = section_def
                current_lines = []
                matched = True
                break

        if not matched and current_section:
            current_lines.append(line)

    if current_section:
        sections.append(ReportSection(
            id=current_section["id"],
            title=current_section["title"],
            content="\n".join(current_lines).strip(),
            status=current_section.get("_status", "ok"),
        ))

    return sections


def _extract_summary(sections: list) -> str:
    """从板块列表中提取核心摘要。"""
    for s in sections:
        if s.id == "summary":
            return s.content
    return sections[0].content if sections else ""


def _extract_first_section(content: str) -> str:
    """从 LLM 输出中提取第一段作为摘要。"""
    lines = content.strip().split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            return stripped[:200]
    return content[:200]


def _rule_based_report(template: dict, slot_results: dict, user_input: str) -> dict:
    """规则化兜底报告（预算不足时使用）。"""
    sections = []

    for section_def in template.get("sections", []):
        status = section_def.get("_status", "ok")
        if status == "skip":
            continue

        sid = section_def["id"]
        title = section_def["title"]

        if status == "degraded":
            content = section_def.get("_note", "数据不足，无法分析")
        else:
            # 拼接插槽数据
            lines = []
            for slot_name in section_def.get("data_slots", []):
                sr = slot_results.get(slot_name)
                if sr and isinstance(sr, SlotResult):
                    lines.append(f"- {sr.interpretation}")
            content = "\n".join(lines) if lines else "暂无数据"

        sections.append(ReportSection(id=sid, title=title, content=content, status=status))

    return {
        "title": template.get("name", "分析报告"),
        "summary": f"关于「{user_input[:20]}」的分析",
        "sections": sections,
        "disclaimer": "以上分析仅供参考，不构成投资建议。市场有风险，投资需谨慎。",
    }

# ── CLI 格式化 ──
COLOR_MAP = {
    "利好": "\033[32m", "利空": "\033[31m",
    "超买": "\033[33m", "超卖": "\033[33m",
    "金叉": "\033[32m", "死叉": "\033[31m",
    "高估": "\033[31m", "低估": "\033[32m",
    "强势": "\033[32m", "弱势": "\033[31m",
}
RESET = "\033[0m"


def format_report(report: dict, color: bool = False) -> str:
    """将结构化报告格式化为文本。color=True 时为 CLI 添加 ANSI 颜色。"""
    lines = []
    sections = report.get("sections", [])
    # 模式 A：单板块完整报告，直接输出内容
    if len(sections) == 1 and sections[0].id == "full_report":
        lines.append(sections[0].content)
        disclaimer = report.get("disclaimer", "")
        if disclaimer:
            lines.append(f"⚠️ {disclaimer}")
        return "\n".join(lines)

    title = report.get('title', '')
    if title:
        lines.append(f"## {title}")
        lines.append("")

    summary = report.get("summary", "")
    if summary:
        lines.append(f"**核心摘要**：{summary}")
        lines.append("")

    for section in sections:
        section_title = section.title if hasattr(section, "title") else section.get("title", "")
        content = section.content if hasattr(section, "content") else section.get("content", "")
        status = section.status if hasattr(section, "status") else section.get("status", "ok")

        if status == "skip":
            continue

        if section_title:
            degraded_mark = " ⚠️(数据不足)" if status == "degraded" else ""
            lines.append(f"### {section_title}{degraded_mark}")
        if status == "degraded" and content and content.strip():
            # degraded 只表示插槽提取率低，不丢弃 LLM 已写出的内容
            lines.append(content)
        elif status == "degraded":
            lines.append("数据不足，无法分析")
        else:
            lines.append(content)
        lines.append("")

    disclaimer = report.get("disclaimer", "")
    if disclaimer:
        lines.append(f"⚠️ {disclaimer}")

    text = "\n".join(lines)
    return _colorize(text) if color else text


def _colorize(text: str) -> str:
    """为关键词添加颜色标记。"""
    for keyword, color in COLOR_MAP.items():
        text = text.replace(keyword, f"{color}{keyword}{RESET}")
    return text
