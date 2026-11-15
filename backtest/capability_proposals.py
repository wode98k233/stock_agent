"""AI 提议扩展引擎能力（用户 2026-10-14 拍板的边界）。

背景：策略由 AI 生成，引擎只提供脚手架。当模型想要「20 日新高」「1.5 倍量」而引擎没有时，
现在的做法是记一条能力缺口 + 在对话里告知用户。本模块补上**下一步**：
把缺口交给 AI，产出**可审阅的扩展提议**（要什么数据、什么公式、什么参数、怎么测）。

⛔ 边界（用户已确认，必须守住）：
1. 只产出**结构化定义 + 说明**，**绝不执行** AI 生成的代码，也不自动注册进引擎；
2. 提议一律进「待审」队列，**人审通过**后才落 approved_registry.json（数据文件，仍不自动生效）；
3. 真正生效仍走开发流程（注册指标表 + 单测 + schema 文案 + 能力清单页），由人做。

一句话：AI 出方案，人拍板，代码由人写。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROPOSAL_PROMPT = """你在为一个「可回测策略引擎」设计能力扩展方案。引擎现状：只支持一批固定指标
（ma/ema/wma/rsi/macd/kdj/boll/atr/dpo/dma/cyw/highest/lowest/volume）、固定数据字段、
比较与交叉算子、以及「持有 N 根后卖出」的纯时序卖点。

模型在生成策略时想要下面这个能力，但引擎没有：
- 类型：%s
- 名称：%s
- 说明：%s

用户的想法（上下文）：%s

请给出一个**可审阅**的扩展方案，只输出 JSON：
{"summary": "一句话说明要加什么",
 "definition": {"semantics": "语义（算什么）", "formula": "公式或伪代码",
                "data_source": "需要的数据字段（如 high/low/volume/成交额/北向资金）",
                "params": [{"name": "参数名", "default": 20, "range": "取值范围", "meaning": "含义"}]},
 "sample_rule_json": {"type": "indicator", "op": ">", "left": {"data": "close"}, "right": {"func": "示例指标", "args": [{"data": "high"}, 20]}},
 "engine_impact": "要动引擎哪里（指标注册/算子/数据字段/时序），一句话",
 "risks": ["风险点，如数据可得性、前视偏差、性能"],
 "test_plan": ["验收要测什么（建议 2-4 条）"]}
"""


def _proposals_dir() -> Path:
    """提议目录（数据文件，非代码；测试可 monkeypatch 本函数）"""
    from utils.app_paths import get_app_dir
    return Path(get_app_dir()) / 'data' / 'capability_proposals'


def _registry_path() -> Path:
    return _proposals_dir() / 'approved_registry.json'


def propose_extension(gap: Dict[str, Any], *, idea: str = '', logger: Any = None) -> Dict[str, Any]:
    """把一个能力缺口交给 LLM 出扩展方案（**不执行、不注册**）。

    返回 {ok, proposal, reason}；LLM 失败 → ok=False（fail-open，调用方如实展示）。
    """
    gap = gap or {}
    kind = str(gap.get('kind') or 'indicator')
    name = str(gap.get('name') or '').strip()
    if not name:
        return {'ok': False, 'reason': 'empty_gap'}
    prompt = _PROPOSAL_PROMPT % (kind, name, str(gap.get('detail') or '（无）'), str(idea or '')[:300])
    # ⚠️ 必须给真 logger：llm_json_with_retry 内部会调 logger.isEnabledFor，
    # 传 None 会 AttributeError（实测踩到过，别再把 None 透进去）
    llm_logger = logger or logging.getLogger(__name__)
    try:
        from utils.llm_factory import get_llm, llm_json_with_retry
        data = llm_json_with_retry(get_llm(), [{'role': 'user', 'content': prompt}],
                                   logger=llm_logger, label='capability-proposal', max_retries=2)
    except Exception as exc:                      # noqa: BLE001
        # ⚠️ 这里的 logger 是**入参**（调用方可能不传），别直接当模块 logger 用
        (logger or logging.getLogger(__name__)).warning('能力扩展提议生成失败：%s', exc)
        # 把真实原因带出去：面板/CLI 要能看到「为什么没生成出来」，不能只给一个笼统的 llm_error
        return {'ok': False, 'reason': 'llm_error', 'error': (type(exc).__name__ + ': ' + str(exc))[:200]}
    if not isinstance(data, dict) or not isinstance(data.get('definition'), dict):
        return {'ok': False, 'reason': 'bad_output'}
    proposal = {
        'kind': kind,
        'name': name,
        'summary': str(data.get('summary') or '')[:300],
        'definition': data.get('definition') or {},
        'sample_rule_json': data.get('sample_rule_json') or {},
        'engine_impact': str(data.get('engine_impact') or '')[:300],
        'risks': [str(x)[:200] for x in (data.get('risks') or [])][:5],
        'test_plan': [str(x)[:200] for x in (data.get('test_plan') or [])][:5],
        'source_gap': {'kind': kind, 'name': name, 'detail': str(gap.get('detail') or '')[:200]},
        'status': 'pending_review',
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    return {'ok': True, 'proposal': proposal}


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in '-_') else '-' for c in str(text)]
    return ''.join(keep).strip('-')[:60] or 'proposal'


def save_proposal(proposal: Dict[str, Any]) -> str:
    """落盘到待审队列，返回文件路径。"""
    path = _proposals_dir() / (str(proposal.get('created_at', '')).replace(':', '').replace(' ', '-')
                               + '-' + _slug(proposal.get('kind', '')) + '-'
                               + _slug(proposal.get('name', '')) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def list_proposals(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出提议（可按 status 过滤：pending_review / approved / rejected）"""
    out: List[Dict[str, Any]] = []
    try:
        for path in sorted(_proposals_dir().glob('*.json')):
            if path.name == 'approved_registry.json':
                continue
            try:
                item = json.loads(path.read_text(encoding='utf-8') or '{}')
            except Exception:
                continue
            item['path'] = str(path)
            if status and str(item.get('status') or '') != status:
                continue
            out.append(item)
    except Exception:
        pass
    return out


def review_proposal(path_or_name: str, decision: str, note: str = '') -> Dict[str, Any]:
    """人审：approved / rejected。

    approved 时**只把定义写进 approved_registry.json**（数据文件）——
    不会改代码、不会让引擎立刻支持它；真正生效仍走开发流程。
    """
    decision = str(decision or '').strip().lower()
    if decision not in ('approved', 'rejected'):
        return {'ok': False, 'reason': 'bad_decision'}
    target = None
    for path in _proposals_dir().glob('*.json'):
        if path.name == 'approved_registry.json':
            continue
        if path.name == path_or_name or str(path) == path_or_name:
            target = path
            break
    if target is None:
        return {'ok': False, 'reason': 'not_found'}
    try:
        item = json.loads(target.read_text(encoding='utf-8') or '{}')
    except Exception:
        return {'ok': False, 'reason': 'bad_json'}
    item['status'] = decision
    item['reviewed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    item['review_note'] = str(note or '')[:300]
    target.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding='utf-8')
    if decision == 'approved':
        registry = []
        try:
            if _registry_path().exists():
                registry = json.loads(_registry_path().read_text(encoding='utf-8') or '[]')
        except Exception:
            registry = []
        registry.append({'kind': item.get('kind'), 'name': item.get('name'),
                         'definition': item.get('definition'), 'approved_at': item['reviewed_at'],
                         'note': item.get('review_note', ''),
                         'effective': False})     # ⛔ 只是「已批准的定义」，还没进引擎
        _registry_path().write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'status': decision, 'path': str(target)}
