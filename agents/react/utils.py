"""ReAct Agent 工具函数

从 nodes.py 中提取的共享工具函数和常量：
- PerToolRateLimiter: 按工具名限流
- 去重相关: _collect_executed_tool_sigs
- 输出精简/压缩: _trim_tool_output, _compress_tool_output
- 历史消息清理: clean_history_messages
- 常量: _TRIM_FIELDS_MX_XUANGU, _TRIM_FIELDS_MX_SEARCH, _COMPRESS_SYSTEM_PROMPT
"""
import asyncio
import json
import re
import time
from collections import defaultdict
from typing import Set, Callable, Awaitable

from langchain_core.messages import AIMessage, ToolMessage

from utils.llm_factory import get_compress_llm, ainvoke_with_retry

# 工具输出压缩的重试次数（不含首次调用）。
#
# 为什么不用 llm_factory._LLM_RETRY_MAX(10)：压缩是**尽力而为的优化**，失败回落原文
# 是安全的（原文仍在 raw_output 里，报告下钻还能取回）；而 10 次 full-jitter 指数退避
# 期望耗时 ~90s、最坏 ~181s。BaseToolNode.compress_tool_output 的「首批预热门闩」会让
# 同批次其余压缩全部阻塞在首条上——一次 429 就能把整批工具调用拖住 3 分钟。
# 3 次退避期望 ~3.5s、最坏 7s，足以吃掉绝大多数 rpm 抖动，又不会拖垮批次。
_TOOL_COMPRESS_MAX_RETRIES = 3


class PerToolRateLimiter:
    """按工具名限流：同名工具串行间隔，不同名工具互不阻塞。"""

    def __init__(
        self,
        min_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._locks = defaultdict(asyncio.Lock)
        self._last_started = {}

    async def wait(self, tool_name: str):
        if self.min_interval_seconds <= 0:
            return

        async with self._locks[tool_name]:
            now = self._clock()
            last_started = self._last_started.get(tool_name)
            if last_started is not None:
                delay = self.min_interval_seconds - (now - last_started)
                if delay > 0:
                    await self._sleep(delay)
            self._last_started[tool_name] = self._clock()


def _collect_executed_tool_sigs(messages: list) -> Set[str]:
    """从消息历史中收集已执行的 tool_name:args 签名（用于迭代间去重提示）"""
    sigs = set()
    for msg in messages:
        # AIMessage 中的 tool_calls 记录了调用意图
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {})
                if name:
                    sigs.add(f"{name}({json.dumps(args, ensure_ascii=False)})")
        # ToolMessage 记录了实际执行结果
        if isinstance(msg, ToolMessage) and msg.name:
            # ToolMessage 没有 args，但 tool_call_id 可以和 AIMessage 的 tool_calls 对应
            # 这里只用 name 做粗粒度过滤不够精确，跳过
            pass
    return sigs


# 冗余字段黑名单：这些字段在 mx_xuangu_filter / mx_search_news 输出中
# 占大量字符但对分析决策贡献极低，精简时移除
_TRIM_FIELDS_MX_XUANGU = {"概念"}
_TRIM_FIELDS_MX_SEARCH = {"result"}  # mx_search 的 result 字段保留，但内部做截断

# mx_data_query 的 tables → markdown 渲染上限（超大表/超多表时截断并标注，
# 后续仍有硬截断（保留两头）与 LLM 压缩（分三类）兜底）
_TRIM_MX_MAX_ROWS_PER_TABLE = 120
_TRIM_MX_MAX_TABLES = 12

# 日期列识别 + 时序行序判定（截断时保证「最近的数据占大头」）
_DATE_COL_HINTS = ("date", "日期", "时间", "trade_date", "report_date")
_YYYYMMDD_RE = re.compile(r"(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})")


def _looks_date_col(name) -> bool:
    n = str(name).lower()
    return any(h in n for h in _DATE_COL_HINTS)


def _rows_date_order(rows: list, fields: list):
    """按含日期的列判断行序：'asc'(早→新) / 'desc'(新→早) / None。"""
    col = next((f for f in fields if _looks_date_col(f)), None)
    if not col:
        return None

    def _d(r):
        s = str(r.get(col, "")) if isinstance(r, dict) else ""
        m = _YYYYMMDD_RE.search(s)
        return m.group(0) if m else None

    first = next((_d(r) for r in rows if _d(r)), None)
    last = next((_d(r) for r in reversed(rows) if _d(r)), None)
    if not first or not last:
        return None
    return "asc" if first <= last else "desc"


def _content_date_order(content: str):
    """从整段文本判断时序：'asc' / 'desc' / None（取首末日期比较）。"""
    ds = [m.group(0) for m in _YYYYMMDD_RE.finditer(content or "")]
    if len(ds) < 2:
        return None
    return "asc" if ds[0] <= ds[-1] else "desc"


def _render_mx_tables_markdown(data: dict) -> str:
    """把 mx_data_query 的 `tables` 渲染成**紧凑 markdown 表格**（保留尽量多的行）。

    背景（2026-09-21 实测）：`terminal_output` 来自 `format_terminal()`，它只渲染
    **第一张表的前 20 行预览**（终端可读性设计）。原 trim 直接 `data.pop("tables")`
    只留 terminal_output，导致 LLM 每次只能看到极少数行 —— 实测一次查询
    `tables` 有 230 行而 terminal_output 只渲染出 7 行；技术指标表 17 行只给 2 行，
    报告因此写「均线、MACD 未返回」（数据其实取到了）。

    这里改为渲染**完整表格**（每表最多 `_TRIM_MX_MAX_ROWS_PER_TABLE` 行、最多
    `_TRIM_MX_MAX_TABLES` 张表，超出显式标注），体积交给下游的
    硬截断（保留两头）+ LLM 压缩（分三类）处理。
    """
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        return ""
    lines = []
    used = 0
    for t in tables:
        if not isinstance(t, dict):
            continue
        if used >= _TRIM_MX_MAX_TABLES:
            lines.append("...（共 %d 张表，已省略后 %d 张）" % (len(tables), len(tables) - used))
            break
        rows = t.get("rows") or []
        fields = t.get("fieldnames") or (
            list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        )
        if not fields:
            continue
        used += 1
        lines.append("### %s（%d 行）" % (str(t.get("sheet_name") or "表")[:60], len(rows)))
        lines.append("| " + " | ".join(str(f) for f in fields) + " |")
        lines.append("| " + " | ".join(["---"] * len(fields)) + " |")
        cap = _TRIM_MX_MAX_ROWS_PER_TABLE
        order = _rows_date_order(rows, fields) if len(rows) > cap else None
        if len(rows) > cap:
            # 时序（有日期列）：升序(早→新)取尾部=最近；降序(新→早)取头部=最近。
            # 非时序（排名/成分股/榜单，无日期列）：保留前 cap 行（榜单"头部"即最重要）。
            shown = rows[-cap:] if order == "asc" else rows[:cap]
            omitted = len(rows) - len(shown)
        else:
            shown, omitted = rows, 0
        for r in shown:
            if isinstance(r, dict):
                lines.append("| " + " | ".join(str(r.get(f, "")) for f in fields) + " |")
        if omitted > 0:
            if order in ("asc", "desc"):
                lines.append("...（本表共 %d 行，已省略较早 %d 行）" % (len(rows), omitted))
            else:
                lines.append("...（本表共 %d 行，已省略其余 %d 行）" % (len(rows), omitted))
        lines.append("")
    return "\n".join(lines).strip()


def _trim_tool_output(tool_name: str, content: str) -> str:
    """精简工具输出：移除冗余字段，不截断有效数据。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content

    if not isinstance(data, dict):
        return content

    if tool_name == "mx_xuangu_filter":
        # 选股结果：移除每只股票的"概念"字段（通常几百字符，列表 10+ 概念标签）
        stocks = data.get("stocks")
        if isinstance(stocks, list):
            for stock in stocks:
                if isinstance(stock, dict):
                    stock.pop("概念", None)

    elif tool_name == "mx_search_news":
        # 搜索结果：保留结构，result 字段中的新闻已经过 API 截断
        # 这里不再二次截断，只移除空字段
        pass

    elif tool_name == "mx_data_query":
        # 数据查询：**不能直接丢 tables** —— terminal_output 只是「第一张表的前 20 行预览」，
        # 丢掉 tables 会让 LLM 只看到极少数行（2026-09-21 实测：230 行 → 7 行，
        # 技术指标 17 行 → 2 行，报告误报「均线、MACD 未返回」）。
        # 改为把 tables 渲染成紧凑 markdown 并把 terminal_output 一并保留（作元信息）。
        rendered = _render_mx_tables_markdown(data)
        if rendered:
            data = {
                "query": data.get("query", ""),
                "tables_count": len(data.get("tables") or []),
                "total_rows": sum(
                    len(t.get("rows") or [])
                    for t in (data.get("tables") or [])
                    if isinstance(t, dict)
                ),
                "terminal_output": data.get("terminal_output", ""),
                "tables": rendered,
            }

    return json.dumps(data, ensure_ascii=False)


# 注意：本提示词必须是**固定内容**（不插入任何变量），跨调用/跨运行保持字节一致，
# 才能命中 provider 的 prompt cache。用户目标由 _compress_tool_output 放进 user 消息。
_COMPRESS_SYSTEM_PROMPT = """你是一个工具输出压缩引擎。你的唯一任务：在不丢失决策信息的前提下，把超长工具输出压缩到原长度的 1/5 ~ 1/3。

你的读者是下游的报告生成 / 仪表盘 LLM：它只能看到你压缩后的文本，看不到原文。你漏掉的信息对它而言等于不存在，你写错的信息会被原样写进分析报告。因此压缩的唯一标准是「信息保真」，而不是「越短越好」。

【零、总则（适用于所有类别）】
1. 只做压缩，不做分析：不写结论、不写投资建议、不添加原文没有的内容。
2. 具体数值必须原样保留：价格、涨跌幅、成交额、金额、比例、日期、条数——严禁把「3.2%」泛化成「上涨」、把「11.45 亿」泛化成「放量」。
3. 命名实体必须保留：股票名称与代码、板块、指数、ETF、机构、产品名、日期。
4. 错误信息原文保留（用反引号包裹），不要"修正"或删减报错详情。
5. 与用户目标（见 user 消息）直接相关的数据优先保留并给足细节；与目标无关的背景铺陈可以大幅删减。
6. 输出直接给压缩后的正文：不要任何前缀、后缀、解释、标题行或"以下是压缩结果"之类的说明。

【一、第一步：判断这段输出属于哪一类，然后按对应策略压缩】

A. 时间序列类（K线/行情/指标时序：含 date、trade_date 等时间字段的一串记录）
   - 优先保留最近时间的数据，越接近今天越重要；较早的数据可大幅合并。
   - 最近 5~10 个周期逐条保留明细；更早的部分不要逐条罗列，改为区间概括：起止日期、区间涨跌幅、最高/最低值及其发生日期、趋势方向与关键转折。
   - 必须标注：原始条数、覆盖区间（首日期 ~ 末日期）。
   - 只报事实，不要因为压缩而写成「数据缺失」。

B. 列表类（选股结果 / 板块与ETF成分股 / 排名榜 / 涨停池：一批标的 + 各自少量字段）
   - 首要目标是保住列表本身：代码与名称尽量完整保留，禁止把列表概括成「若干只股票」。
   - 每项的字段可以裁剪（只留决策相关列），但不要主动削减条目数量。
   - 优先完整展示：① 龙头 / 涨幅居前 ② 用户持仓或关注列表中出现过的标的。
   - 条目确实过多时必须截断：保留前 N 条并标注「共 M 条，已省略 X 条」；如果原输出已自带省略标注，原样保留该标注。

C. 文本类（新闻 / 研报 / 财报 / 公告 / 政策 / 搜索结果）
   - 提高信息熵：只留事实、数字、时间、主体、影响判断；删掉修辞、免责声明、重复表述、页脚与格式化装饰。
   - 同一主题的多条合并为一条要点；与用户目标直接相关的优先保留并给足细节。
   - 每条尽量保留：标题/主体 + 日期 + 来源；关键数字与日期按原文保留。

D. 表格/宽表类（Excel 风格宽表：大量字段 × 若干行；如财务指标表、资金流明细、基本面组合表）
   - 保留关键列：时间/标的/名称 + 数值列；删除明显无关的技术噪音列（如内部代码 CHOICE_INNER_CODE、IN_OPTIONAL、重复的分类字段、全部为空的列）。
   - 行数多时按 A 类策略处理（时序取最近、排名取头部），并在表头或表尾标注原始行列数与省略情况。
   - 输出成紧凑 Markdown 表格；数值单元格按原文保留单位。

E. 其他 / 无法归类：按最接近的一类处理，优先保事实与数值。

【二、输出格式规范】
1. 使用 Markdown；表格用紧凑表格（不要为了排版补全空格）。
2. 开头或结尾给出元信息：原始字符数/条数、覆盖区间（日期/排名范围）、省略情况。
3. 省略必须显式标注：如「共 320 条，已省略中间 280 条」「已省略较早 24 行」——不允许静默丢弃。
4. 长列表按"每条一行"压缩要点，避免大段散文复述。
5. 输出长度目标：原长度的 1/5 ~ 1/3；如果原文已经很短，直接原样返回。

【三、硬规则（违反任何一条都视为压缩失败）】
1. 绝不编造信息；绝不把具体数值泛化成模糊描述。
2. 错误信息原文保留（用反引号包裹）。
3. 保留命名实体：股票名称与代码、板块、指数、日期。
4. 无法归入 A/B/C/D 时，按最接近的一类处理。
5. 严格区分「主动截断」与「数据缺失」（最高优先级）：
   - 文中出现「主动截断」「已省略中间 N 字符」等标记 → 表示数据已被工具取到，只是为控制上下文而裁剪。这类内容绝不能写成「数据缺失」，应表述为「因输出过长未全量载入（可补取）」。
   - 只有工具明确返回错误 / 空结果 / 接口失败时，才算「数据缺失」。
   - 不得由「某段被截断」推断「其它周期/字段也缺失」（实测错误：把「省略日K中段」错写成「周K/月K 数据缺失」）。
6. 直接输出压缩后的内容，不要任何前缀或解释。

【四、示例】

示例 1（A 类 · 时序）：
原文结构（约 260 行日K，2025-08-27 ~ 2026-09-21）：
| date | open | close | volume |
| 2025-08-27 | 12.1 | 12.3 | 8.2万手 |
| ...（中间 250 行省略）... |
| 2026-09-19 | 15.8 | 16.1 | 12.4万手 |
| 2026-09-21 | 16.1 | 16.5 | 13.1万手 |
压缩后示例：
「日K共 260 条，覆盖 2025-08-27 ~ 2026-09-21。区间：收盘 12.3 → 16.5（+34.1%），区间最高 16.6（2026-09-18），最低 11.8（2025-10-14）。最近 5 日：09-15 收 15.9（+1.2%）、09-16 收 16.0（+0.6%）、09-17 收 15.7（-1.9%）、09-18 收 16.4（+4.5%）、09-21 收 16.5（+0.6%）；成交量近 3 日 11.2 / 12.4 / 13.1 万手，逐步放大。」

示例 2（B 类 · 列表）：
原文结构（20 只涨停股，每只含 20+ 字段）...
压缩后示例：保留全部代码与名称，字段只留「代码 / 名称 / 连板 / 涨停原因 / 封单额」；与用户目标相关的标的给足细节，其余从简；结尾标注「共 20 条，全部保留」。

示例 3（C 类 · 新闻）：
原文结构（多条 INV_NEWS，含免责声明、排版符号）...
压缩后示例：合并为主题要点，逐条给「日期 · 标题 · 关键事实（数字 / 主体）」，删除免责声明与重复表述。

示例 4（D 类 · 宽表）：
原文结构（1 行 × 40 列财务指标，含大量内部字段）...
压缩后示例：只留「报告期 + 营收/净利/毛利率/ROE 等核心指标」，其余列删除并标注「原始 40 列，已省略技术字段 28 列」。

（示例只示范压缩风格；实际处理时以原文内容为准，不要照抄示例数字。）

【五、常见工具输出形态速查（本系统）】
- mx_data_query（数据查询）：tables[] + terminal_output。多张表时逐表处理，保留表名（sheet_name）与字段名；数值保留原单位（亿 / 万 / %）。
- mx_xuangu_filter（选股器 dataList）：stocks[] 每只 20+ 字段。按 B 类处理：代码 / 名称 / 涨跌幅 / 成交额 / 换手率 / 量比 / 主力净额优先；删除概念标签长文本与内部字段。
- 涨停池 / 炸板池 / 连板天梯（JSON 列表）：按 B 类处理，保留 代码 / 名称 / 连板数 / 封单额 / 涨停时间 / 涨停原因；炸板池保留 open_times（炸板次数）。
- 龙虎榜：保留营业部名称 / 买入额 / 卖出额 / 净额；机构席位与游资席位分开保留。
- 新闻搜索（result 长文本）：按 C 类处理，逐条保留日期 / 标题 / 主体 / 关键数字。
- 板块与指数行情、技术指标表：按 A / D 类处理；指标名称写全（如 MACD 柱状线、RSI 相对强弱指标）。
- 持仓 / 资金流水：按 B 类，金额字段必须原样保留（含正负号与单位）。

【六、边界与特殊情况】
1. 本提示词只处理「输出太长」这一种情况。工具报错 / 空结果的原文本来就短，直接原样返回，不要美化或改写错误信息。
2. 原文是 JSON 而你想转表格：可以转换，但语义不能变；字段名保持中文键名的含义（可精简措辞，不得翻译错位）。
3. 原文包含多段相互独立的子结果（多张表、多条榜单混排）：逐段压缩，并在段首标明来源（表名 / 榜单名 / 序号）。
4. 数值精度保持原文：原文几位小数就保留几位，不要四舍五入；原文为字符串形式的"-"或空值照原样保留。
5. 时间口径写清楚：区分「日期」与「日期时间」，注明数据时点（如 11:31 快照 / 收盘口径），不要把盘中快照写成收盘值。

【七、最后自查（输出前逐条确认）】
1. 是否漏了任何具体数值 / 日期 / 代码 / 主体？
2. 是否把任何「已取到但被裁剪」的内容写成了「数据缺失」？
3. 是否添加了原文没有的内容或结论？
4. 元信息（条数 / 区间 / 省略情况）是否已标注？
5. 输出是否无前缀、无后缀、可直接嵌入下游上下文？"""


def _hard_truncate_keep_both_ends(content: str, limit: int, head_ratio: float = 0.15) -> str:
    """硬截断：保留「头部（字段名/表头等元信息）+ 尾部（最近数据）」并**标注省略量**。

    原实现是 `content[:limit]`（纯头截断）。对**按时间升序**的序列（如
    `get_etf_history` 的 K 线：最早在前、最新在后）会恰好砍掉最近的数据 ——
    2026-09-21 实测：260 行日K（2025-08-27~2026-09-21）被截成只剩最早一段，
    报告据此误报「日K未覆盖当日序列」（数据本已完整覆盖当日）。

    保留两头可同时兼容两种排序：升序序列的「最近」在尾部、降序（最新在前）的
    在头部。中段按字符省略并显式标注。

    标注语义（2026-09-22 修正）：必须让下游分清**主动截断**与**数据缺失**——
    这里砍掉是因为**输出超长、为控制上下文而主动裁剪**，数据本身**已被工具取到**；
    而「数据缺失」指源头没给。前者应写成「主动截断（可补取）」，**不得写成「数据缺失」**
    （实测：报告把「省略日K中段 24,735 字符」错写成「周K/月K 数据缺失」）。
    """
    if limit <= 0 or len(content) <= limit:
        return content
    # 按日期序自适应：升序(早→新)留尾部(最近)占大头；降序(新→早)留头部(最近)占大头。
    # 非时序(None)沿用默认（尾部占大头）。
    order = _content_date_order(content)
    if order == "desc":
        head = min(max(200, int(limit * (1 - head_ratio))), limit // 2)
    else:
        head = min(max(200, int(limit * head_ratio)), limit // 2)
    tail = limit - head
    omitted = len(content) - head - tail
    return (content[:head]
            + (f"\n...[主动截断：原始输出 {len(content)} 字符，为控制上下文在此省略中段 "
               f"{omitted} 字符（已优先保留最近数据）。数据已由工具取到，仅未全量载入。"
               f"此段属于**主动截断**（可补取），**不得写成「数据缺失」**]...\n")
            + content[-tail:])


async def _compress_tool_output(tool_name: str, content: str, logger, user_goal: str = "") -> str:
    """对超长工具输出进行 LLM 压缩，保留关键信息。

    系统提示固定不变（跨调用/跨运行字节一致），用户目标放进 user 消息 ——
    保证 prompt cache 的公共前缀足够长且稳定。2026-09-28 实测：原实现把可变的
    goal_hint 拼进 system 且前缀仅 ~566 token，压缩调用缓存命中率恒为 0；
    且前缀过短低于 provider 最小可缓存块，串行/并行都无法命中。

    重试：走 ainvoke_with_retry，429/5xx/网络抖动按 full-jitter 指数退避重试
    _TOOL_COMPRESS_MAX_RETRIES 次（2026-10-10 修：原实现直接 llm.ainvoke，遇 429
    立刻吞掉并回落原文，导致 provider 限流时压缩整批失效）。重试耗尽仍失败则
    静默回落原文——压缩是可选优化，不该让工具执行失败。
    """
    try:
        llm = get_compress_llm()
        user_parts = []
        if user_goal:
            user_parts.append(
                f"用户最终目标：{user_goal}\n请优先保留与上述目标直接相关的数据，删除无关背景信息。"
            )
        user_parts.append(f"工具: {tool_name}")
        user_parts.append(f"输出内容:\n{content}")
        messages = [
            ("system", _COMPRESS_SYSTEM_PROMPT),
            ("user", "\n\n".join(user_parts)),
        ]
        config = {"metadata": {"node": "react-tool-compress"}}
        response = await ainvoke_with_retry(
            llm, messages, config=config, logger=logger,
            label="react-tool-compress", max_retries=_TOOL_COMPRESS_MAX_RETRIES,
        )
        compressed = response.content if hasattr(response, 'content') else str(response)
        if compressed and len(compressed) < len(content):
            ratio = len(content) / len(compressed) if len(compressed) > 0 else 0
            logger.debug("C", f"工具输出压缩: {tool_name} {len(content)}→{len(compressed)}字符 ({ratio:.1f}x)")
            return compressed
        logger.warning("C", f"工具输出压缩无效: {tool_name} 原文{len(content)}→压缩{len(compressed) if compressed else 0}字符，保留原文")
        return content
    except Exception as e:
        logger.warning("C", f"工具输出压缩失败: {tool_name} ({len(content)}字符), 保留原始输出: {e}")
        return content


def clean_history_messages(history: list, user_input: str) -> list:
    """清理历史消息，移除无效和重复内容。

    清理规则：
    1. 过滤空的 AI 消息（无 content 且无 tool_calls）
    2. 移除所有与 user_input 完全相同的用户消息（PrepareNode 末尾会追加当前 user）
    3. 过滤包含 "[用户问题]" 标记且内含 user_input 的消息

    Args:
        history: 原始历史消息列表（可包含 AIMessage、ToolMessage、(role, content) 元组等）
        user_input: 当前用户输入，用于识别重复内容

    Returns:
        清理后的历史消息列表
    """
    cleaned_history = []
    seen_current_user = False

    for item in history:
        if isinstance(item, AIMessage) and not item.content and not (item.tool_calls or []):
            continue

        if isinstance(item, tuple) and len(item) == 2 and item[0] in ("user", "human"):
            content = str(item[1] or "")
            if content == user_input:
                if seen_current_user:
                    continue
                seen_current_user = True
                continue
            if "[用户问题]" in content and user_input in content:
                continue

        cleaned_history.append(item)

    return cleaned_history
