"""
同花顺问财 Skills 集成
约定标准接口，供 tools.skill_register 自动发现和加载

参照 eastmoney/skills.py 的模式：
- 读取每个子目录的 SKILL.md 获取元数据和目录文本
- TOOL_REGISTRY 定义工具名 → (core_func, ParamModel) 映射
- get_skill_loaders() 返回 {skill_name: build_func}，build_func 创建 @tool 装饰的 LangChain tool

约定接口：
- get_skill_catalog() -> str
- get_skill_loaders() -> dict
- build_all_tools(logger, memory_mgr) -> list
- TOOL_REGISTRY

设计要点（2026-09-20 收敛）
--------------------------------
问财 SkillHub 的 26 个官方技能包实测**只有两种真实能力**（见 openspec 记录）：

- 22 个 ``hithink-*`` 查询/筛选类共用 ``POST /v1/query2data``，请求体完全相同
- 3 个 ``*-search`` 检索类共用 ``POST /v1/comprehensive/search``，仅 ``channels`` 不同
- 1 个 ``模拟炒股`` 是独立的交易账户体系（开户/下单），不接入

且网关**不校验** ``X-Claw-Skill-Id``：用错配的、甚至不存在的 Skill-Id 请求，
返回体与正确 Skill-Id 完全一致。即该 Header 只是问财侧的统计标识，不参与路由
（路由由 ``query`` 文本 / ``channels`` 决定）。

因此本模块把工具收敛为 2 个统一入口，覆盖全部技能包能力：

- ``iwc_query``  -> ``/v1/query2data``，选股/行情/财务/指数/行业/宏观/事件/股东/评级/…
- ``iwc_search`` -> ``/v1/comprehensive/search``，channel ∈ news / announcement / report

每个技能目录仍各自注册为 skill（目录层可选、usage_guide 按需加载），但它们声明的
工具名统一指向上面两个；``SkillRegister.build_all_langchain_tools`` 按工具名去重，
最终暴露给 LLM 的工具恒为 2 个。

SKILL.md 契约段（``## 工具列表`` / ``## 目录层信息``）由
``scripts/ops/patch_iwencai_skill_contracts.py`` 统一生成，新增技能包后需重跑该脚本。

配额归属（``X-Claw-Skill-Id``）
------------------------------
官方口径是「每个 skill 每天 100 次」。既然网关不校验该 id 的存在性（实测），
记账媒介就只能是它本身——请求端点、请求体、其余 header 全同，没有第二个可区分维度。
但工具收敛后**工具名不再等于技能名**，请求层无法反推该报哪个 id，于是：

- 运行级上下文 ``utils/skill_ctx.py`` 记录「当前激活技能」；
  写入点在 ``agents/shared/tool_utils.py::activate_skill``（渐进披露唯一入口）
  与 ``SkillRegister.execute_tool``（直调路径）；
- ``_resolve_skill_id`` 读它并把下划线名转回 dash 形式（``hithink_finance_query``
  → ``hithink-finance-query``），**仅当该名字属于本包的技能集合时才采用**；
- 取不到时退回 ``_DEFAULT_SKILL_ID``（工具名），保证行为与收敛前一致不会报错。

额度防护对齐妙想 ``tools/other_skills/eastmoney/mx_common.py``：进程级 1.5s 节流、
频控（429 / 操作过于频繁）退避重试且 ``X-Claw-Call-Type=retry``、
**额度耗尽不重试**（按日/周恢复，重试只会白打请求并空等退避）。
"""
import os
import json
import logging
import re
import secrets
import threading
import time
from typing import Literal

import requests
import yaml
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config import Config
from utils.app_paths import get_external_skills_dir

logger = logging.getLogger("radar.iwencai")

IWENCAI_SKILLS_ROOT = os.path.join(get_external_skills_dir(), "10jqka")

# ── 问财 API 常量 ──────────────────────────────────────────────────────────
_DEFAULT_BASE_URL = "https://openapi.iwencai.com"
_ENDPOINT_SEARCH = "/v1/comprehensive/search"
_ENDPOINT_QUERY2DATA = "/v1/query2data"
_APP_ID = "AIME_SKILL"
_SKILL_VERSION = "1.0.0"

# 统一工具名
TOOL_QUERY = "iwc_query"
TOOL_SEARCH = "iwc_search"

# iwc_search 允许的渠道
SEARCH_CHANNELS = ("news", "announcement", "report")

# 统一工具名 → 兜底统计标识（无法确定域时的最后退路）
_DEFAULT_SKILL_ID = {TOOL_QUERY: TOOL_QUERY, TOOL_SEARCH: TOOL_SEARCH}


# ── 节流 / 额度防护 ────────────────────────────────────────────────────────
# 对齐妙想 `tools/other_skills/eastmoney/mx_common.py` 的实战口径（该模式是被真实打爆过
# 才沉淀出来的）：进程级最小间隔 + 区分「可重试的频控」与「不可重试的额度耗尽」。
#
# 问财与妙想是两个独立后端、各自计费，故持有**各自独立**的节流时钟，不共用全局间隔。
_MIN_INTERVAL_S = 1.5
_MAX_RETRIES = 2
_RETRY_SLEEPS = (2.0, 5.0)

# 额度耗尽特征（不可重试：按日/周恢复，重试必然失败，只会多打请求并空等退避）
_QUOTA_MARKERS = (
    "次数已达到上限",
    "额度已达到上限",
    "已达到上限",
    "额度将于",
    "额度已用完",
    "今日调用次数已达上限",
    "调用次数已达上限",
)

# 频控特征（可重试）
_RATE_LIMIT_MARKERS = (
    "操作过于频繁",
    "请求频率过高",
    "请求过于频繁",
    "too many requests",
)

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """进程级最小请求间隔，抑制突发（并发批次逐个放行）。"""
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_at + _MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _response_texts(payload) -> list:
    """取出响应里可能承载错误文案的字段（问财错误可能落在顶层或 data 内层）。"""
    if not isinstance(payload, dict):
        return [str(payload or "")]
    texts = [
        str(payload.get("message") or ""),
        str(payload.get("status_msg") or ""),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        texts.append(str(data.get("status_msg") or ""))
        texts.append(str(data.get("message") or ""))
    return texts


def is_quota_exhausted(payload) -> bool:
    """识别「额度/次数耗尽」（不可重试）。"""
    return any(
        any(marker in text for marker in _QUOTA_MARKERS)
        for text in _response_texts(payload)
    )


def is_rate_limited(payload, status_code=None) -> bool:
    """识别频控（可重试）：HTTP 429 或业务层「操作过于频繁」。

    调用方须先用 `is_quota_exhausted` 排除额度耗尽，再决定是否重试。
    """
    if status_code == 429:
        return True
    if isinstance(payload, dict) and payload.get("status_code") == 429:
        return True
    return any(
        any(marker in text for marker in _RATE_LIMIT_MARKERS)
        for text in _response_texts(payload)
    )


def _resolve_skill_id(explicit: str = None) -> str:
    """决定本次请求上报的 ``X-Claw-Skill-Id``。

    优先级：显式入参 → 运行级上下文里的「当前激活技能」→ 工具名兜底。

    上下文来源见 `utils/skill_ctx.py`：工具收敛为 2 个统一入口后，工具名不再等于技能名，
    底层请求层无法从工具名反推该报哪个 skill id。问财配额口径是「每 skill 每天 100 次」，
    若全部请求共用一个 id，N 个技能的额度会被挤进同一个桶。
    """
    if explicit:
        return str(explicit)
    try:
        from utils.skill_ctx import get_active_skill
        name = get_active_skill()
        # 仅当该名字确实是问财技能时才采用，避免把 mx_data 之类的名字当成问财 skill id
        if name and name in _iwc_skill_loaders:
            return name.replace("_", "-")
    except Exception:
        pass
    return ""


def _default_skill_id(tool_name: str) -> str:
    return _DEFAULT_SKILL_ID.get(tool_name, tool_name)


# ── Pydantic 参数模型 ─────────────────────────────────────────────────────
class IWCQueryParams(BaseModel):
    """问财通用查询（query2data）参数"""

    query: str = Field(
        ...,
        description='自然语言查询问句，如"市盈率小于20且市值大于100亿的股票"、"贵州茅台近三年ROE"',
    )
    page: str = Field("1", description="页码，从 1 开始")
    limit: str = Field("10", description="每页条数，默认 10")


class IWCSearchParams(BaseModel):
    """问财检索（comprehensive/search）参数"""

    query: str = Field(..., description='搜索问句，如"贵州茅台研报"、"贵州茅台 分红公告"')
    channel: Literal["news", "announcement", "report"] = Field(
        "news", description="搜索渠道：news 新闻 / announcement 公告 / report 研报"
    )
    size: int = Field(10, description="返回条数，默认 10")


# ── 统一 API 调用 ──────────────────────────────────────────────────────────
def _build_headers(api_key: str, skill_id: str, call_type: str = "normal") -> dict:
    """构造问财网关要求的 Claw 系列 header（重试时 call_type 改 `retry`）。"""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Claw-Call-Type": call_type,
        "X-Claw-Skill-Id": skill_id,
        "X-Claw-Skill-Version": _SKILL_VERSION,
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def _post_iwencai(endpoint: str, payload: dict, query: str, skill_id: str) -> str:
    """问财 API 公共请求逻辑（query2data 和 comprehensive/search 共用）。

    行为对齐官方文档与妙想的实战口径：
    - 进程级最小间隔节流（问财自有配额，与妙想独立计数）；
    - 频控（HTTP 429 / 「操作过于频繁」）退避重试，重试时 ``X-Claw-Call-Type=retry``；
    - **额度耗尽不重试**（按日/周恢复，重试只会多打请求并空等退避）；
    - 其余异常维持原行为（立即返回错误 JSON，由调用方降级）。
    """
    api_key = Config.IWENCAI_API_KEY
    if not api_key:
        return json.dumps({"error": "IWENCAI_API_KEY 未配置", "status": "failed"}, ensure_ascii=False)

    base_url = Config.IWENCAI_BASE_URL or _DEFAULT_BASE_URL
    url = f"{base_url}{endpoint}"

    def _wrap(data, **extra):
        result = {"query": query, "skill_id": skill_id, "data": data}
        if "channels" in payload:
            result["channels"] = payload["channels"]
        result.update(extra)
        return json.dumps(result, ensure_ascii=False)

    for attempt in range(_MAX_RETRIES + 1):
        is_retry = attempt > 0
        headers = _build_headers(api_key, skill_id, "retry" if is_retry else "normal")
        _throttle()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
        except requests.exceptions.Timeout:
            return json.dumps({"query": query, "error": "请求超时", "status": "failed"}, ensure_ascii=False)
        except requests.exceptions.ConnectionError:
            return json.dumps({"query": query, "error": "连接失败", "status": "failed"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"query": query, "error": str(e), "status": "failed"}, ensure_ascii=False)

        try:
            data = resp.json()
        except ValueError:
            return json.dumps({
                "error": "invalid_json_response",
                "status_code": resp.status_code,
                "raw": resp.text[:2000],
            }, ensure_ascii=False)

        # 额度耗尽：直接返回，重试无意义（省下请求次数 + 退避空等）
        if is_quota_exhausted(data):
            return _wrap(data, status="failed", error="同花顺问财额度已耗尽（按日/周恢复，重试无意义）")

        if is_rate_limited(data, resp.status_code):
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_SLEEPS[min(attempt, len(_RETRY_SLEEPS) - 1)])
                continue
            # 退避重试用尽仍未放行：显式标失败，避免把频控载荷当成「查无结果」
            return _wrap(data, status="failed",
                         error=f"同花顺问财持续限流（已重试 {_MAX_RETRIES} 次）")

        if is_retry:
            data = dict(data) if isinstance(data, dict) else {"raw": data}
            data["_retried"] = attempt
        return _wrap(data)

    # 兜底（正常路径均在上方 return；保留以防后续改动破坏循环出口）
    return _wrap({}, status="failed", error="同花顺问财请求失败")


# ── 统一 core 函数 ─────────────────────────────────────────────────────────
def _iwc_query_core(query: str, page: str = "1", limit: str = "10", skill_id: str = None) -> str:
    """统一查询入口：POST /v1/query2data

    覆盖问财全部 22 个 ``hithink-*`` 技能（选股/行情/财务/指数/行业/宏观/事件/
    股东股本/机构评级/基本资料/经营数据/板块与 ETF 筛选等）。网关按 query 文本路由。

    skill_id 未显式指定时，由 `_resolve_skill_id` 从运行级上下文取「当前激活技能」，
    以便按 skill 上报配额归属（问财口径：每 skill 每天 100 次）。
    """
    payload = {
        "query": query,
        "page": str(page),
        "limit": str(limit),
        "is_cache": "1",
        "expand_index": "true",
    }
    return _post_iwencai(
        _ENDPOINT_QUERY2DATA, payload, query,
        _resolve_skill_id(skill_id) or _default_skill_id(TOOL_QUERY),
    )


def _iwc_search_core(query: str, channel: str = "news", size: int = 10, skill_id: str = None) -> str:
    """统一检索入口：POST /v1/comprehensive/search

    channel 决定检索通道：news 新闻 / announcement 公告 / report 研报。
    """
    if channel not in SEARCH_CHANNELS:
        return json.dumps({
            "query": query,
            "error": f"channel 非法: {channel!r}，仅支持 {list(SEARCH_CHANNELS)}",
            "status": "failed",
        }, ensure_ascii=False)

    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 10

    payload = {"query": query, "channels": [channel], "app_id": _APP_ID, "size": size}
    return _post_iwencai(
        _ENDPOINT_SEARCH, payload, query,
        _resolve_skill_id(skill_id) or _default_skill_id(TOOL_SEARCH),
    )


# ── TOOL_REGISTRY ──────────────────────────────────────────────────────────
TOOL_REGISTRY = {
    TOOL_QUERY: (_iwc_query_core, IWCQueryParams),
    TOOL_SEARCH: (_iwc_search_core, IWCSearchParams),
}


# ── SKILL.md 解析 + Skill 注册元数据 ───────────────────────────────────────
_TOOL_SECTION_RE = re.compile(r"^##\s*工具列表\s*$", re.M)
_TOOL_HEADER_RE = re.compile(r"^###\s*工具\d+[：:]\s*(\S+)", re.M)
_FN_DESC_RE = re.compile(r"^-\s*功能[：:]\s*(.+)", re.M)

_iwc_skill_meta = {}
_iwc_skill_loaders = {}
_iwc_catalog = ""


# 统一工具的规范描述：刻意做成「域无关」，因为工具是全局唯一的；
# 具体域（选股/财务/研报…）由各技能 SKILL.md 的 usage_guide 与技能目录负责。
_TOOL_DESCRIPTIONS = {
    TOOL_QUERY: (
        "同花顺问财数据查询：把自然语言问句转成结构化数据表。"
        "覆盖选股筛选、个股/ETF/指数行情、财务指标、行业数据、宏观指标、公司事件、"
        "股东股本、机构评级与一致预期、基础资料、经营数据、板块与 ETF 筛选等。"
        "参数：query（自然语言查询问句，必填）、page（页码，默认 1）、limit（每页条数，默认 10）。"
        "返回 columns + datas 结构化表格，code_count 为命中总数；数据来源同花顺问财。"
    ),
    TOOL_SEARCH: (
        "同花顺问财资讯检索：按 channel 检索财经新闻 / 上市公司公告 / 卖方研报。"
        "参数：query（搜索问句，必填）、channel（news 新闻 | announcement 公告 | report 研报）、"
        "size（返回条数，默认 10）。"
        "返回 data 数组，元素含 title / summary / url；数据来源同花顺问财。"
    ),
}


def _parse_declared_tool(content: str):
    """从 SKILL.md 的 ``## 工具列表`` 段解析声明的统一工具名与功能描述。

    解析口径必须与 ``tools/skill_register.py::_parse_tool_list`` 一致——**段头必需**，
    否则会出现「skills.py 认得出工具、注册器却解析出 0 个工具」的静默错配。

    返回 (tool_name, fn_desc)；段头缺失或工具名未登记时返回 (None, "")。
    """
    section = _TOOL_SECTION_RE.search(content)
    if not section:
        return None, ""
    body = content[section.end():]
    nxt = re.search(r"^##\s", body, re.M)
    if nxt:
        body = body[:nxt.start()]

    m = _TOOL_HEADER_RE.search(body)
    if not m:
        return None, ""
    tool_name = m.group(1).strip()
    if tool_name not in TOOL_REGISTRY:
        return None, ""
    fn = _FN_DESC_RE.search(body)
    return tool_name, (fn.group(1).strip() if fn else "")


def _load_iwencai_single_skill(skill_dir):
    """加载单个问财技能（参照 eastmoney/_load_eastmoney_single_skill）"""
    skill_name = os.path.basename(skill_dir)
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(skill_md_path):
        return
    try:
        with open(skill_md_path, 'r', encoding='utf-8-sig') as f:
            md_content = f.read()
        if md_content.startswith('---'):
            parts = md_content.split('---', 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1].strip()) or {}
                content = parts[2].strip()
            else:
                meta = {"name": skill_name, "description": ""}
                content = md_content
        else:
            meta = {"name": skill_name, "description": ""}
            content = md_content

        actual_skill_name = str(meta.get("name", skill_name)).replace("-", "_")
        skill_desc = str(meta.get("description", "") or "")

        tool_name, fn_desc = _parse_declared_tool(content)
        if not tool_name:
            logger.warning(
                "问财技能 %s 的 SKILL.md 未声明有效工具（需 ## 工具列表 / ### 工具N：iwc_query|iwc_search），"
                "请重跑 scripts/ops/patch_iwencai_skill_contracts.py", skill_name,
            )
            return

        description = _TOOL_DESCRIPTIONS[tool_name]

        def build_func(logger_obj, memory_mgr, _tool_name=tool_name, _desc=description):
            core_func, param_model = TOOL_REGISTRY[_tool_name]

            def wrapper(**kwargs):
                return core_func(**kwargs)

            wrapper.__name__ = _tool_name
            wrapper.__qualname__ = _tool_name
            return [tool(description=_desc, args_schema=param_model)(wrapper)]

        _iwc_skill_meta[actual_skill_name] = meta
        _iwc_skill_loaders[actual_skill_name] = build_func

        # 构建 catalog 文本（仅作为 loader key 无对应 SKILL.md 时的兜底）
        global _iwc_catalog
        one_line = (fn_desc or skill_desc).split("。")[0]
        _iwc_catalog += (
            f"\n### {actual_skill_name}({one_line})\n"
            f"- 统一工具：{tool_name}"
        )
    except Exception as e:
        logger.error(f"加载问财技能 {skill_name} 失败: {e}")


def _discover_iwencai_skills():
    """自动发现并加载所有问财技能（排序保证 loader 顺序确定）"""
    global _iwc_catalog
    _iwc_catalog = "## 同花顺问财增强技能"
    if not os.path.exists(IWENCAI_SKILLS_ROOT):
        return
    for item in sorted(os.listdir(IWENCAI_SKILLS_ROOT)):
        item_path = os.path.join(IWENCAI_SKILLS_ROOT, item)
        if os.path.isdir(item_path) and not item.startswith('.') and item != '__pycache__':
            _load_iwencai_single_skill(item_path)


_discover_iwencai_skills()


# ── 标准接口 ───────────────────────────────────────────────────────────────
def get_skill_catalog():
    return _iwc_catalog


def get_skill_loaders():
    return _iwc_skill_loaders.copy()


def build_all_tools(logger_obj, memory_mgr):
    """构建全部问财工具（同名工具按名去重，最终恒为 2 个）"""
    tools = []
    seen = set()
    if not Config.IWENCAI_API_KEY:
        logger_obj.warning("IWENCAI_API_KEY 未配置，同花顺问财 Skills 不可用")
        return tools
    for skill_name, loader in _iwc_skill_loaders.items():
        try:
            for t in loader(logger_obj, memory_mgr) or []:
                if t.name not in seen:
                    seen.add(t.name)
                    tools.append(t)
        except Exception as e:
            logger_obj.warning(f"加载问财技能 {skill_name} 失败: {e}")
    return tools
