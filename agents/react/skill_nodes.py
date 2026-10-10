"""ReAct 技能渐进披露：子目标路由与失败兜底重选。"""
from langchain_core.runnables import RunnableConfig

from agents.prompts import SKILL_GAP_PROMPT, SKILL_ROUTER_PROMPT
from agents.shared.tool_utils import activate_skills
from utils.agent_trace.core import get_active_recorder
from utils.llm_factory import llm_json_with_retry
from utils.logger import ensure_radar


def _compose_batch(active: str, candidates: list, n: int) -> list:
    """新 active 打头，其后补候选池（去重保序），截到 n（n<=0 全量）。"""
    ordered = [active] + [c for c in (candidates or []) if c and c != active]
    return ordered if n <= 0 else ordered[:n]


def _compact_catalog(registry) -> tuple[str, set]:
    """紧凑技能目录（名 + 首句描述）——供 router 在候选池不够时回退挑选。"""
    try:
        items = registry.get_skill_catalog() or []
    except Exception:
        return "", set()
    lines, names = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("skill_name") or "")
        if not nm:
            continue
        names.add(nm)
        desc = (it.get("description") or "").strip().split("，")[0][:40]
        lines.append(f"- {nm}: {desc}")
    return "\n".join(lines), names


def _batch_size() -> int:
    try:
        from config import Config
        return int(getattr(Config, "REACT_SKILL_BATCH_SIZE", 3))
    except Exception:
        return 3


def _reselect_every() -> int:
    """每 N 轮工具后回 router 重新组批（<=0 关闭；默认 1=每轮）。"""
    try:
        from config import Config
        return int(getattr(Config, "REACT_SKILL_RESELECT_EVERY", 1))
    except Exception:
        return 1


def _exec_summary(state: dict, limit: int = 6) -> str:
    """最近工具调用的一行摘要，供路由判断。"""
    lines = []
    for call in (state.get("messages") or [])[-limit:]:
        for tc in (getattr(call, "tool_calls", None) or []):
            lines.append(f"{tc.get('name')}({tc.get('args')})")
    return "\n".join(lines) or "（暂无）"


def _market_for(state: dict) -> str:
    """按用户输入识别市场（import 收口，router/gap 共用）。"""
    from tools.tool_capabilities import infer_market_from_text
    return infer_market_from_text(state.get("user_input", ""))


class SubGoalRouterNode:
    """每轮判断当前子目标需要哪些技能（可中途换批）；变化时原子替换 tools_ref。

    append_usage=True（中途换批节点）：批次变化时把**新增技能**的 L1 用法作为一条
    system 消息追加进上下文（初始批次的用法由 prepare 注入，此处只补增量）。
    """

    def __init__(self, llm, skill_registry, tools_ref=None, budget=None, logger=None,
                 append_usage: bool = False, allow_catalog_fallback: bool = False):
        self.llm = llm
        self.skill_registry = skill_registry
        self.tools_ref = tools_ref if tools_ref is not None else []
        self.budget = budget
        self.logger = ensure_radar(logger)
        self.append_usage = append_usage
        # 候选池不够时，允许 router 从全量技能目录里补一个（并并入候选池）
        self.allow_catalog_fallback = allow_catalog_fallback

    async def __call__(self, state: dict, config: RunnableConfig | None = None) -> dict:
        candidates = [s for s in (state.get("candidate_skills") or []) if s]
        active = state.get("active_skill") or ""
        loaded = [s for s in (state.get("loaded_skills") or []) if s in candidates]
        sub_goals = list(state.get("sub_goals") or [])

        if not candidates:
            if not active:
                return {"active_skill": "", "sub_goals": sub_goals, "loaded_skills": []}
            new_active, desired = active, []
        elif len(candidates) == 1 and active != candidates[0]:
            new_active, desired = candidates[0], [candidates[0]]
        elif active and active in candidates:
            new_active, sub_goals, desired = await self._route_with_llm(
                state, active, candidates, sub_goals, loaded, config)
        else:
            new_active = active or candidates[0]
            desired = loaded or ([new_active] if new_active else [])

        # 目录兜底：router 可能从候选池外补入技能 → 并入候选池（后续轮次可见）
        if desired:
            candidates = list(dict.fromkeys(
                candidates + [s for s in desired if s and s not in candidates]))
        # 期望批次为空 → 退回「新 active + 候选」的默认批量；裁剪到上限并去重
        if not desired:
            desired = _compose_batch(new_active, candidates, _batch_size()) if new_active else []
        desired = self._trim(desired, candidates, new_active)

        changed = desired != loaded
        if changed:
            names = activate_skills(self.skill_registry, self.tools_ref, desired,
                                    selected_skills=candidates, logger=self.logger,
                                    market=_market_for(state), allow_all_fallback=False)
            # 初次激活 / 主技能切换 / 仅换批
            kind = "activate" if not active else ("switch" if new_active != active else "reselect")
            self._record(kind, {"active_skill": new_active, "from": active,
                                "candidates": candidates, "tools": names,
                                "loaded_skills": desired})
            self.logger.info("R", f"技能批{'装载' if not active else '调整'}: "
                                 f"{loaded or '-'} → {desired}（主 {new_active}）")
        elif new_active and not self.tools_ref:
            activate_skills(self.skill_registry, self.tools_ref, desired,
                            selected_skills=candidates, logger=self.logger,
                            market=_market_for(state), allow_all_fallback=False)

        out = {"active_skill": new_active, "sub_goals": sub_goals,
               "loaded_skills": desired, "candidate_skills": candidates}
        # 中途换批：把新增技能的 L1 用法追加为一条 system 消息（初始批由 prepare 注入）
        if self.append_usage and changed and active:
            delta = self._usage_delta([s for s in desired if s not in loaded])
            if delta:
                out["messages"] = [("system", delta)]
        return out

    @staticmethod
    def _trim(desired, candidates, active) -> list:
        """去重、限定在候选池内、截到上限；保证主技能在内。"""
        n = _batch_size()
        seen, out = set(), []
        for s in desired:
            if s in candidates and s not in seen:
                seen.add(s)
                out.append(s)
        if n > 0:
            out = out[:n]
        if active and active in candidates and active not in out:
            out = [active] + out
            if n > 0:
                out = out[:n]
        return out

    def _usage_delta(self, added: list) -> str:
        if not added:
            return ""
        from tools.skills import SkillPromptBuilder
        parts = []
        for sk in added:
            try:
                d = SkillPromptBuilder.build_tools_detail_prompt(self.skill_registry, sk)
                if d:
                    parts.append(d)
            except Exception:
                pass
        if not parts:
            return ""
        return "【本轮新增技能用法（中途换批）】\n\n" + "\n\n".join(parts)

    async def _route_with_llm(self, state, active, candidates, sub_goals, loaded, config):
        n = _batch_size()
        allowed = set(candidates)
        catalog_block = ""
        if self.allow_catalog_fallback:
            catalog_block, extra = _compact_catalog(self.skill_registry)
            allowed |= extra  # 允许路由从目录补一个技能
        prompt = (
            f"{SKILL_ROUTER_PROMPT.replace('{max_n}', str(n if n > 0 else len(candidates)))}\n"
            f"用户问题：{state.get('user_input', '')}\n"
            f"当前主技能：{active}\n"
            f"候选技能：{', '.join(candidates)}\n"
            f"已装载技能：{loaded or '[]'}\n"
            f"最近工具调用：\n{_exec_summary(state)}\n"
            f"当前子目标：{sub_goals or '[]'}"
            + (f"\n\n若候选池都不合适，可从此技能目录里补选（返回其名即可，会并入候选池）：\n{catalog_block}"
               if catalog_block else "")
        )
        try:
            result = llm_json_with_retry(
                self.llm, [("system", prompt)], self.logger,
                label="skill-router", budget=self.budget,
                metadata={"node": "skill-router"},
            )
        except Exception:
            result = None
        if not isinstance(result, dict):
            return active, sub_goals, loaded
        picked = str(result.get("active_skill") or "")
        if picked not in allowed:
            picked = active
        goals = [str(g) for g in (result.get("sub_goals") or []) if str(g).strip()]
        raw = result.get("loaded_skills")
        desired = [s for s in raw if s in allowed] if isinstance(raw, list) else []
        return picked, (goals or sub_goals), desired

    def _record(self, kind: str, data: dict) -> None:
        try:
            recorder = get_active_recorder()
            if recorder is not None:
                recorder.record_skill_event(kind, data)
        except Exception:
            self.logger.debug("R", "技能事件记录失败（忽略）")


class SkillGapNode:
    """工具连续失败后的兜底重选：候选池优先，其次用全目录交给 LLM。"""

    MAX_GAPS = 2

    def __init__(self, llm, skill_registry, tools_ref=None, budget=None, logger=None):
        self.llm = llm
        self.skill_registry = skill_registry
        self.tools_ref = tools_ref if tools_ref is not None else []
        self.budget = budget
        self.logger = ensure_radar(logger)

    async def __call__(self, state: dict, config: RunnableConfig | None = None) -> dict:
        count = int(state.get("skill_gap_count") or 0)
        active = state.get("active_skill") or ""
        if count >= self.MAX_GAPS:
            return {"skill_gap_count": count}

        candidates = [s for s in (state.get("candidate_skills") or []) if s and s != active]
        valid = []
        getter = getattr(self.skill_registry, "get_skill_meta", None)
        for name in candidates:
            meta = getter(name) if callable(getter) else None
            if not callable(getter) or meta is not None:
                valid.append(name)
        candidates = valid
        new_active = ""
        reason = ""
        if candidates:
            new_active, reason = candidates[0], "候选池切换"
        else:
            new_active, reason = await self._pick_from_catalog(state, active)
            if new_active:
                reason = "全目录重选"

        if not new_active or new_active == active:
            self._record({"active_skill": active, "from": active, "to": "",
                          "trigger": "工具连续失败", "succeeded": False})
            return {"skill_gap_count": count + 1}

        batch = _compose_batch(new_active, state.get("candidate_skills"), _batch_size())
        names = activate_skills(self.skill_registry, self.tools_ref, batch,
                                selected_skills=state.get("candidate_skills"), logger=self.logger,
                                market=_market_for(state), allow_all_fallback=False)
        self._record({"active_skill": new_active, "from": active, "to": new_active,
                      "trigger": "工具连续失败", "reason": reason, "tools": names,
                      "loaded_skills": batch})
        self.logger.info("R", f"技能重选: {active or '-'} → {new_active}（{reason}，批 {batch}）")
        return {"active_skill": new_active, "skill_gap_count": count + 1, "loaded_skills": batch}

    async def _pick_from_catalog(self, state, active):
        try:
            from tools.skills import SkillPromptBuilder
            catalog = SkillPromptBuilder.build_catalog_prompt(self.skill_registry)
            getter = getattr(self.skill_registry, "get_skill_catalog", None)
            raw_items = getter() if callable(getter) else []
            allowed = {str(item.get("skill_name") or "")
                       for item in raw_items if isinstance(item, dict)}
        except Exception:
            catalog, allowed = "", set()
        if not catalog:
            return "", ""
        prompt = (f"{SKILL_GAP_PROMPT}\n当前技能：{active or '-'}\n"
                  f"用户问题：{state.get('user_input', '')}\n{catalog}")
        try:
            result = llm_json_with_retry(
                self.llm, [("system", prompt)], self.logger,
                label="skill-gap", budget=self.budget, metadata={"node": "skill-gap"},
            )
        except Exception:
            result = None
        if not isinstance(result, dict):
            return "", ""
        picked = str(result.get("active_skill") or "")
        if picked not in allowed:
            return "", ""
        return (picked if picked and picked != active else ""), str(result.get("reason") or "")

    def _record(self, data: dict) -> None:
        try:
            recorder = get_active_recorder()
            if recorder is not None:
                recorder.record_skill_event("gap_reselect", data)
        except Exception:
            self.logger.debug("R", "技能事件记录失败（忽略）")


def make_route_after_tools(exec_state, max_gaps: int = None):
    """tools 出口条件路由。

    优先级：失败兜底（gap）> 周期性换批（reselect）> 继续 agent。
    - gap：最近一轮工具**全部失败**且未超限。
    - reselect：距上次不足 `REACT_SKILL_RESELECT_EVERY` 轮则跳过；候选池里还有未装载
      技能时，每 N 轮回 router 重新组批（用完一批换下一批）。
    """
    limit = SkillGapNode.MAX_GAPS if max_gaps is None else max_gaps

    def _route(state: dict) -> str:
        if int(state.get("skill_gap_count") or 0) < limit and _last_iteration_failed(state, exec_state):
            return "gap"
        every = _reselect_every()
        loaded = state.get("loaded_skills") or []
        candidates = state.get("candidate_skills") or []
        if every > 0 and len(loaded) < len(candidates):
            it = int(state.get("iteration_count") or 0)
            if it > 0 and it % every == 0:
                return "reselect"
        return "agent"

    return _route


def _last_iteration_failed(state: dict, exec_state=None) -> bool:
    """最近一条 AI 消息之后的 ToolMessage 是否全部失败。

    以消息内容判定（is_tool_success 口径），天然覆盖：同轮去重（复制 ToolMessage）、
    exact 复用（内容复制）、工具不存在（"执行失败: 未找到工具"）、结构化 error。
    """
    messages = state.get("messages") or []
    idx = None
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "tool_calls", None):
            idx = i
            break
    if idx is None:
        return False
    tool_msgs = [m for m in messages[idx + 1:] if getattr(m, "tool_call_id", None)]
    if not tool_msgs:
        return False
    from agents.executor_callbacks import is_tool_success
    return all(not is_tool_success(str(getattr(m, "content", ""))) for m in tool_msgs)
