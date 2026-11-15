"""M1 编排器：大纲 → 分节写作（串行）→ 汇编 → 静态阅读页。

刻意**不放进 LangGraph**（设计 §1）：1.5 万字挂 state 会膨胀，且单节重跑要可寻址。
取消：进程内 threading.Event，每节边界检查（M2 换成 cancel_check 注入）。
"""
from __future__ import annotations

import logging
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from agents.deep_report import models as M
from agents.deep_report.outliner import build_outline
from agents.deep_report.render import build_report_html, write_html
from agents.deep_report.store import DeepReportStore
from agents.deep_report.templates import load_template
from agents.deep_report.writer import write_section

from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 返工/审稿这类"锦上添花"的调用必须有硬超时：卡住就保留原文，不能拖垮整份报告
_REVISE_TIMEOUT_MAX = 90
_MIN_REVIEW_S = 120        # 剩余预算低于此值就不再审稿/返工（保证报告能收尾）

# trace 归因：长报告的所有 LLM 调用都属于同一个逻辑任务（agent_name='deep_report'），
# 细分到调用点（outline / write-N / review）靠 metadata.label。
_TRACE_AGENT_NAME = "deep_report"
_TRACE_PURPOSE = "deep_report"


def _trace_recorder_factory():
    """按配置创建长报告 trace recorder；未开启 trace 返回 None。

    独立成模块级函数是为了可测：单测 monkeypatch 它注入 FakeRecorder，
    不依赖真实 Config / SQLite。任何异常都在这里吞掉（trace 失败不能影响报告）。
    """
    try:
        from config import Config
        if not getattr(Config, "ENABLE_TRACE", False):
            return None
        from utils.agent_trace.core import TraceRecorder
        # TRACE_DB_PATH 显式指定 → 直写单文件（调试）；默认 None → 分库路由当月月库
        db_path = getattr(Config, "TRACE_DB_PATH", "") or None
        return TraceRecorder(agent_name=_TRACE_AGENT_NAME, db_path=db_path)
    except Exception:
        logger.warning("长报告 trace 初始化失败（不影响报告产出）", exc_info=True)
        return None


def _label_of_callbacks(callbacks) -> str:
    """从 tracked_invoke 组装的 callbacks 里读回本次调用的 label。

    ``tracked_invoke`` 只把 label 交给 TokenRecorder，**不会**写进 config.metadata；
    而 TraceRecorder 的 ``extra.label`` 只认 metadata（见 core.build_llm_step_extra）。
    不补这一层，steps 里就只有模型类名，看不出是「第几章写作 / 审稿 / 选图」。
    """
    try:
        from utils.token_recorder import TokenRecorder
        handlers = getattr(callbacks, "handlers", None)
        if handlers is None and isinstance(callbacks, (list, tuple)):
            handlers = callbacks
        for handler in handlers or []:
            if isinstance(handler, TokenRecorder):
                return str(getattr(handler, "label", "") or "")
    except Exception:
        pass
    return ""


class _TraceLabelLLM:
    """给 LLM 调用补 trace 归因 metadata（purpose / label）。

    长报告的调用点（outliner/writer/review/chart_candidates）不在本任务可改范围内，
    但都走 ``utils.llm_factory.tracked_invoke``。这里包一层：从 config.callbacks 的
    TokenRecorder 读出 label，补进 metadata，trace 就能按调用点归因，调用点零改动。
    """

    def __init__(self, llm, *, default_label: str = _TRACE_PURPOSE):
        self._llm = llm
        self._default_label = default_label

    def invoke(self, messages, config=None, **kwargs):
        cfg = dict(config or {})
        metadata = dict(cfg.get("metadata") or {})
        metadata.setdefault("purpose", _TRACE_PURPOSE)
        label = _label_of_callbacks(cfg.get("callbacks")) or self._default_label
        if label:
            metadata.setdefault("label", label)
        cfg["metadata"] = metadata
        return self._llm.invoke(messages, config=cfg, **kwargs)

    def __getattr__(self, name):
        # bind_tools / with_structured_output 等能力原样透传
        return getattr(self._llm, name)


def _close_trace_entry(entry) -> None:
    """关闭工作线程里 attach 的 recorder（reset ContextVar + close 连接）。"""
    if not entry:
        return
    recorder, token = entry
    try:
        from utils.agent_trace.core import _active_recorder
        _active_recorder.reset(token)
    except Exception:
        pass
    try:
        recorder.close()
    except Exception:
        pass


def _with_timeout(fn, seconds: int, default=None, *, trace_entry=None):
    """跑 fn 最多 seconds 秒；超时返回 default。

    每个调用用**独立 daemon 线程**，不用共享线程池：一次挂死的 LLM 调用会永久占住
    池里的 worker（评审：2 线程池被吞掉后，后续所有报告的审稿都跑不了）。
    超时后线程继续跑完即退出，不会再阻塞别的任务。
    """
    def _call():
        entry = None
        try:
            # ContextVar 不跨线程：工作线程必须自己 attach 同一 run，否则审稿/返工
            # 的 LLM 调用不会落进报告 run（看起来接了 trace，实际读不到）。
            entry = trace_entry() if trace_entry is not None else None
            return fn()
        finally:
            _close_trace_entry(entry)

    box: Dict[str, Any] = {}

    def _runner():
        try:
            box["value"] = _call()
        except BaseException as e:  # noqa: BLE001 —— 线程里的异常不能穿透
            box["error"] = e

    th = threading.Thread(target=_runner, name="deep-revise", daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        logger.warning("返工/审稿超时（>%ss），保留原结果", seconds)
        return default
    if "error" in box:
        logger.warning("返工/审稿失败，保留原结果: %s", box["error"])
        return default
    return box.get("value")


class DeepReportRuntime:
    def __init__(self, *, store: Optional[DeepReportStore] = None, llm=None, log=None,
                 progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 executor=None, fetch: bool = False, base_dir=None, db=None,
                 qa_enabled: bool = True, semantic_review: bool = True,
                 deadline_s: float = 900.0, use_capabilities: bool = True):
        self.store = store or DeepReportStore(base_dir, db=db)
        # trace 归因代理：不改变调用点，只把 label/purpose 补进 config.metadata
        self.llm = _TraceLabelLLM(llm) if llm is not None else None
        self.log = log or logger
        self._progress = progress
        self.executor = executor
        self.fetch = fetch
        self.qa_enabled = qa_enabled
        self.semantic_review = semantic_review
        self.deadline_s = deadline_s
        self.use_capabilities = use_capabilities
        # 本次 run 的 trace 状态（run() 里初始化；线程池工作线程靠它 attach）
        self._trace_run_id: Optional[str] = None
        self._trace_db_path: str = ""

    # ── 事件 ──
    def _emit(self, rid: str, event: str, payload: Dict[str, Any]) -> None:
        try:
            self.store.append_event(rid, event, payload)
        except Exception:  # 事件写失败不能拖垮报告
            logger.warning("事件落盘失败 rid=%s event=%s", rid, event, exc_info=True)
        if self._progress:
            try:
                self._progress(event, payload)
            except Exception:
                logger.warning("进度回调异常", exc_info=True)

    def _catalog_md(self, outline, chosen, seq: int) -> str:
        """目录与图表目录：程序生成，标题带上真实章号，保证 1..N 连续。"""
        lines = [f"## {seq}. 目录与图表目录",
                 "", "| 章 | 标题 | 目标字数 | 主要图表 |", "|---|---|---|---|"]
        for s in outline.sections:
            lo, hi = (s.target_words + [0, 0])[:2]
            charts = "、".join(s.charts) if s.charts else "—"
            mark = "" if (s in chosen or s.section_id == "catalog") else "（本章未生成）"
            lines.append(f"| {s.seq} | {s.title}{mark} | {lo}–{hi} | {charts} |")
        return "\n".join(lines) + "\n"

    # ── trace ──
    def _thread_trace_entry(self):
        """工作线程入口：attach 到当前报告 run（ContextVar 不跨线程）。

        每线程独建 recorder（各自的 SQLite 连接），**复用主 recorder 已选定的月库**，
        避免跨月 run 的 steps 被写到另一个月库、读侧按 run 所在月库查不到。
        """
        run_id = self._trace_run_id
        if not run_id:
            return None
        try:
            from utils.agent_trace.core import TraceRecorder, _active_recorder
            recorder = TraceRecorder(agent_name=_TRACE_AGENT_NAME,
                                     db_path=self._trace_db_path or None)
            recorder.attach_run(run_id)   # 不新建 runs 行、不负责 end_run
            token = _active_recorder.set(recorder)
            return recorder, token
        except Exception:
            logger.warning("长报告线程 trace 挂载失败（忽略）", exc_info=True)
            return None

    def _traced_fetch(self, svc, slot: str, universe: str, cancel, *,
                      capability: bool = False, symbol: str = "",
                      candidates: Optional[List[Dict[str, Any]]] = None,
                      deadline: float = 120.0,
                      tiers: Optional[Dict[str, List[Dict[str, str]]]] = None):
        """取数子代理的线程入口：把它的 LLM 调用也记进同一个报告 run。"""
        entry = self._thread_trace_entry()
        try:
            if capability:
                return svc.fetch_capability(slot, universe, cancel=cancel, symbol=symbol,
                                            candidates=candidates, deadline_s=deadline,
                                            tiers=tiers)
            return svc.fetch(slot, universe, cancel=cancel, deadline_s=deadline)
        finally:
            _close_trace_entry(entry)

    def _persist_trace_run_id(self, rid: str, run_id: str) -> None:
        """回写 deep_report.trace_run_id（列已存在；db.py 未提供单列更新入口）。"""
        if not rid or not run_id:
            return
        db = getattr(self.store, "db", None)
        ctx = getattr(db, "_ctx", None)
        if ctx is None:
            return
        try:
            with ctx() as conn:
                conn.execute("UPDATE deep_report SET trace_run_id=? WHERE rid=?",
                             (run_id, rid))
        except Exception:
            logger.warning("长报告 trace_run_id 回写失败 rid=%s", rid, exc_info=True)

    def _finish_trace(self, recorder, token, meta, error: str, rid: str) -> None:
        """run 收尾：写 status/duration，并复位 ContextVar、关闭连接（全程不抛）。"""
        if recorder is None:
            return
        if self._trace_run_id:
            self._persist_trace_run_id(rid, self._trace_run_id)
        try:
            status = "error"
            output: Dict[str, Any] = {}
            if isinstance(meta, dict):
                output = {k: meta.get(k) for k in
                          ("rid", "status", "words", "sections_done",
                           "sections_total", "charts")}
                report_status = str(meta.get("status") or "")
                if report_status in (M.STATUS_DONE, M.STATUS_PARTIAL):
                    status = "success"
                elif report_status == M.STATUS_CANCELLED:
                    status = "cancelled"
            recorder.end_run(output=output, status=status, error=error)
        except Exception:
            logger.warning("长报告 trace 收尾失败（忽略）", exc_info=True)
        finally:
            try:
                from utils.agent_trace.core import _active_recorder
                if token is not None:
                    _active_recorder.reset(token)
            except Exception:
                pass
            try:
                recorder.close()
            except Exception:
                pass

    def run(self, *, universe: str, user_request: str = "",
            template_id: str = "deep_sector_finance",
            max_sections: Optional[int] = None,
            cancel: Optional[threading.Event] = None,
            rid: Optional[str] = None,
            dialog_uuid: str = "") -> Dict[str, Any]:
        """编排入口（trace 外壳）：建 run → _run → 收尾。

        trace 的任何失败都只记日志，绝不改变报告产出（设计 §9：观测不能反噬业务）。
        """
        rid = rid or self.store.new_rid()
        self._trace_run_id = None
        self._trace_db_path = ""
        recorder = None
        trace_token = None
        try:
            recorder = _trace_recorder_factory()
            if recorder is not None:
                self._trace_run_id = recorder.start_run(task_input={
                    "rid": rid, "universe": universe, "template_id": template_id,
                    "user_request": user_request,
                })
                # 线程池 worker 必须写同一个库（跨月不漂移）
                self._trace_db_path = str(getattr(recorder, "_db", "") or "")
                from utils.agent_trace.core import _active_recorder
                trace_token = _active_recorder.set(recorder)
        except Exception:
            logger.warning("长报告 trace 初始化失败（不影响报告产出）", exc_info=True)
            # 半初始化（run 已建、ContextVar 未挂上）也要收尾，避免留下永远 running 的 run
            if recorder is not None:
                try:
                    recorder.end_run(status="error", error="trace init failed")
                except Exception:
                    pass
                try:
                    recorder.close()
                except Exception:
                    pass
            recorder = None
            trace_token = None
            self._trace_run_id = None

        meta: Optional[Dict[str, Any]] = None
        error = ""
        try:
            meta = self._run(universe=universe, user_request=user_request,
                             template_id=template_id, max_sections=max_sections,
                             cancel=cancel, rid=rid, dialog_uuid=dialog_uuid)
            return meta
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._finish_trace(recorder, trace_token, meta, error, rid)

    def _run(self, *, universe: str, user_request: str = "",
             template_id: str = "deep_sector_finance",
             max_sections: Optional[int] = None,
             cancel: Optional[threading.Event] = None,
             rid: Optional[str] = None,
             dialog_uuid: str = "") -> Dict[str, Any]:
        template = load_template(template_id)
        rid = rid or self.store.new_rid()
        t0 = time.time()

        meta = M.ReportMeta(rid=rid, universe=universe, user_request=user_request,
                            template_id=template_id, variant=str(template.get("variant", "")),
                            dialog_uuid=dialog_uuid,
                            status=M.STATUS_OUTLINING, stage="outline")
        self.store.create(meta)
        # trace 链接尽早回写：进程中途被杀也能从报告行找回 run
        self._persist_trace_run_id(rid, self._trace_run_id or "")
        self._emit(rid, "phase", {"stage": "outline", "progress": 2})

        try:
            outline = build_outline(template, universe=universe, user_request=user_request,
                                    llm=self.llm, log=self.log)
        except Exception as e:  # 模板/LLM 都失败 → 明确失败，不产出空壳
            meta = self.store.finish(rid, M.STATUS_FAILED, error=f"outline: {e}", stage="outline")
            self._emit(rid, "error", {"stage": "outline", "message": str(e)})
            return meta.to_dict()

        self.store.save_outline(rid, outline)
        # 目录章由程序生成，不调模型；max_sections 针对**可写章节**计数（先过滤再切片）
        writable = [s for s in outline.sections if s.section_id != "catalog"]
        chosen = writable[:max_sections] if max_sections else list(writable)
        total = len(chosen)

        # ── 取数：能力优先（Step 2），失败回落旧槽链 ──
        cards: Dict[str, dict] = {}
        sec_keys: Dict[int, List[str]] = {}
        if self.fetch and self.executor is not None:
            from agents.deep_report.capabilities import SLOT_TO_CAPABILITY, all_names, from_slot

            cap_slots: Dict[str, List[str]] = {}
            for slot, cap in SLOT_TO_CAPABILITY.items():
                if cap:
                    cap_slots.setdefault(cap, []).append(slot)

            keys: List[str] = []
            tiers = template.get("stock_tiers") or {}
            for sec in chosen:
                use_caps = bool(self.use_capabilities and sec.capabilities)
                if use_caps:
                    sec_keys[sec.seq] = [c for c in sec.capabilities if c in all_names()]
                else:
                    sec_keys[sec.seq] = [s for s in sec.data_slots if s]
                for k in sec_keys[sec.seq]:
                    if k not in keys:
                        keys.append(k)

            # 标的解析：技能工具要代码；**歧义交给子代理按语义选**（候选列表随任务下发）
            resolved = ""
            candidates: List[Dict[str, Any]] = []
            if self.use_capabilities:
                from agents.deep_report.resolver import resolve as _resolve, search as _search
                q_tool = getattr(self.executor, "tools", {}).get("iwc_query")

                def _query(text: str) -> str:
                    if q_tool is None:
                        return ""
                    return q_tool.invoke({"query": text}) if hasattr(q_tool, "invoke") else q_tool(text)

                try:
                    candidates = _search(universe)[:5]
                    resolved = _resolve(universe, db_path=None,
                                        query_fn=(_query if q_tool is not None else None))
                except Exception:
                    logger.warning("标的解析失败，能力取数将使用原话", exc_info=True)
                if resolved:
                    self._emit(rid, "resolve", {"universe": universe, "symbol": resolved,
                                                "candidates": [c["code"] for c in candidates]})

            meta = self.store.update(rid, status=M.STATUS_WRITING, stage="fetching", progress=5)
            self._emit(rid, "phase", {"stage": "fetching", "progress": 5,
                                      "keys": len(keys), "mode": "capability" if self.use_capabilities else "slot"})
            from agents.deep_report.fetch import FetchService
            svc = FetchService(executor=self.executor, store=self.store, rid=rid, log=self.log)
            workers = max(1, min(4, len(keys)))
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_one(key: str) -> Dict[str, Any]:
                """能力 → fetch_capability；失败只回落到**首个**映射旧槽（限 60s）。

                ⚠️ 不能把所有映射槽都试一遍：consensus 实测 3 个槽 × 120s = 5.5 分钟，
                把整份报告的时间预算吃光。宁可快一点报缺口，也不要慢着等失败。
                """
                if key in all_names() and self.use_capabilities:
                    card = self._traced_fetch(svc, key, universe, cancel, capability=True,
                                              symbol=resolved, candidates=candidates,
                                              tiers=tiers)
                    if card.get("status") != "ok":
                        for slot in cap_slots.get(key, [])[:1]:
                            fallback = self._traced_fetch(svc, slot, universe, cancel,
                                                          deadline=60.0)
                            if fallback.get("status") == "ok":
                                return fallback
                    return card
                return self._traced_fetch(svc, key, universe, cancel)

            done_n = 0
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deep-fetch")
            try:
                futs = {pool.submit(_fetch_one, k): k for k in keys}
                for fut in as_completed(futs):
                    key = futs[fut]
                    try:
                        card = fut.result()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("取数任务失败 key=%s: %s", key, e)
                        continue
                    cards[key] = card
                    done_n += 1
                    self._emit(rid, "subagent", {"slot": key, "ev_id": card.get("ev_id"),
                                                 "status": card.get("status"),
                                                 "items": len(card.get("items") or []),
                                                 "index": done_n, "total": len(keys)})
                    self.store.update(rid, progress=5 + int(20 * done_n / max(1, len(keys))))
                    if cancel is not None and cancel.is_set():
                        break
            finally:
                # 取消/退出时不等卡住的取数线程（with 退出会 join，取消按钮会假死）
                pool.shutdown(wait=False, cancel_futures=True)
            if cancel is not None and cancel.is_set():
                self.store.update(rid, status=M.STATUS_CANCELLED, stage="fetching", error="cancelled")
                return self.store.read_meta(rid).to_dict()

        meta = self.store.update(rid, title=outline.title, sections_total=total,
                                 status=M.STATUS_WRITING, stage="writing",
                                 progress=25 if cards else 5)
        self._emit(rid, "phase", {"stage": "writing", "progress": 25 if cards else 5,
                                  "sections_total": total, "outline_source": outline.source,
                                  "evidence": len(cards)})

        results: List[M.SectionResult] = []
        # 目录章由程序生成（不调模型），但必须占住自己的 seq，否则 1→3 空号
        catalog_sec = next((s for s in outline.sections if s.section_id == "catalog"), None)
        if catalog_sec is not None:
            catalog_md = self._catalog_md(outline, chosen, catalog_sec.seq)
            results.append(M.SectionResult(catalog_sec.seq, catalog_sec.section_id,
                                           catalog_sec.title, M.SEC_OK,
                                           content=catalog_md,
                                           words=len(catalog_md)))
            catalog_sec.status = M.SEC_OK
            self.store.save_section(rid, results[-1])
        failed = 0
        for idx, sec in enumerate(chosen, start=1):
            if cancel is not None and cancel.is_set():
                self.store.update(rid, status=M.STATUS_CANCELLED, stage="writing",
                                  error="cancelled", sections_done=idx - 1)
                self._emit(rid, "done", {"status": M.STATUS_CANCELLED, "sections_done": idx - 1})
                return self.store.read_meta(rid).to_dict()

            sec.status = M.SEC_WRITING
            self._emit(rid, "section", {"seq": sec.seq, "title": sec.title,
                                        "status": M.SEC_WRITING, "index": idx, "total": total})
            result = write_section(outline, sec, template=template,
                                   prev=[r for r in results if r.content],
                                   evidence=[cards[k] for k in (sec_keys.get(sec.seq) or sec.data_slots)
                                             if k in cards],
                                   universe=universe, llm=self.llm, log=self.log,
                                   cancel_check=(cancel.is_set if cancel else None))
            results.append(result)
            if result.status == M.SEC_OK:
                sec.status = M.SEC_OK
                sec.actual_words = result.words
                self.store.save_section(rid, result)
            else:
                sec.status = M.SEC_FAILED
                sec.error = result.error
                failed += 1

            done = idx
            base = 25 if cards else 5
            progress = base + int((90 - base) * done / max(1, total))
            self.store.update(rid, sections_done=done, progress=progress, stage=f"writing {done}/{total}")
            self._emit(rid, "progress", {"progress": progress, "sections_done": done,
                                         "sections_total": total})

        # ── 附录：数据缺口声明（程序生成，不占模型） ──
        gaps: List[str] = []
        for card in cards.values():
            for g in (card.get("gaps") or []):
                if str(g) not in gaps:
                    gaps.append(str(g))
        if gaps:
            lines = ["## 数据缺口声明", "", "（正文仅在必要处标注「数据缺失」，完整清单在此）", ""]
            lines += [f"- {g}" for g in gaps[:15]]
            appendix = M.SectionResult(99, "appendix", "数据缺口声明", M.SEC_OK,
                                       content="\n".join(lines) + "\n", words=len("\n".join(lines)))
            results.append(appendix)
            self.store.save_section(rid, appendix)

        # ── 统稿：确定性校验 → 调度审稿 → 单节返工 → 再校验 ──
        qa: Dict[str, Any] = {"checks": [], "conflicts": [], "passed": True}
        review_info: Dict[str, Any] = {}
        expected_ids = [s.section_id for s in chosen]
        if self.qa_enabled:
            from agents.deep_report.qa import run_qa
            from agents.deep_report.review import review as review_report

            qa = run_qa(results, outline, cards, expected_ids=expected_ids)
            self._emit(rid, "qa", {"passed": qa["passed"], "conflicts": len(qa["conflicts"]),
                                   "checks": [c["id"] for c in qa["checks"] if not c["passed"]]})

            # 语义审稿：即使程序校验全过也要跑（thesis 与章节结论可能互斥）
            remaining = self.deadline_s - (time.time() - t0)
            need_review = (not qa["passed"]) or (self.semantic_review and self.llm is not None)
            if need_review and remaining >= _MIN_REVIEW_S:
                review_info = _with_timeout(
                    lambda: review_report(outline, results, qa, llm=self.llm, log=self.log,
                                          semantic=self.semantic_review),
                    int(min(120, remaining - 30)),
                    default={"verdict": "skipped", "rewrites": [], "source": "timeout",
                             "reason": "审稿超时，保留原文"},
                    trace_entry=self._thread_trace_entry) or {}
                # 章节有证据、thesis 站不住 → 优先改 thesis（审稿建议）
                boss_changed = False
                if review_info.get("thesis_fix"):
                    outline.thesis = str(review_info["thesis_fix"])[:120]
                    boss_changed = True
                    self._emit(rid, "review", {"status": "thesis_fixed",
                                               "thesis": outline.thesis})
                if review_info.get("title_fix"):
                    new_title = str(review_info["title_fix"])[:40]
                    if new_title and new_title != outline.title:
                        outline.title = new_title
                        self.store.update(rid, title=new_title)
                        boss_changed = True
                        self._emit(rid, "review", {"status": "title_fixed", "title": new_title})
                if boss_changed:
                    self.store.save_outline(rid, outline)
                    # 主线/标题变了 → 摘要必须跟着改，否则首屏就是自相矛盾
                    summ = next((r for r in results if r.section_id == "summary" and r.content), None)
                    summ_spec = next((s for s in outline.sections if s.section_id == "summary"), None)
                    remaining2 = self.deadline_s - (time.time() - t0)
                    if summ is not None and summ_spec is not None and self.llm is not None \
                            and remaining2 >= 120:
                        new_summ = _with_timeout(
                            lambda: write_section(
                                outline, summ_spec, template=template, prev=[], evidence=[],
                                universe=universe, llm=self.llm, log=self.log,
                                cancel_check=(cancel.is_set if cancel else None),
                                revision={"fix": "按新的投资逻辑与标题重写摘要，"
                                                 f"标题：{outline.title}；主线：{outline.thesis}",
                                          "keep": "保留原有数字与来源，不得新增未经证据支持的数字"},
                                previous=summ.content[:6000]),
                            int(min(_REVISE_TIMEOUT_MAX, remaining2 - 60)),
                            trace_entry=self._thread_trace_entry)
                        if new_summ is not None and new_summ.status == M.SEC_OK and new_summ.content:
                            results = [new_summ if r.seq == new_summ.seq else r for r in results]
                            self.store.save_section(rid, new_summ)
                            self._emit(rid, "review", {"seq": new_summ.seq,
                                                       "status": "summary_rewritten",
                                                       "words": new_summ.words})
                if review_info.get("semantic_issues"):
                    self._emit(rid, "review", {"status": "semantic_issues",
                                               "issues": review_info["semantic_issues"]})
            elif need_review:
                review_info = {"verdict": "skipped", "rewrites": [], "source": "budget",
                               "reason": f"剩余 {int(remaining)}s 不足审稿"}
                self._emit(rid, "review", {"verdict": "skipped", "reason": "budget"})
            rewrites = review_info.get("rewrites") or []
            self._emit(rid, "review", {"verdict": review_info.get("verdict"),
                                       "source": review_info.get("source"),
                                       "rewrites": len(rewrites)})
            for rw in rewrites[:2]:
                target = next((r for r in results if r.seq == rw.get("seq") and r.content), None)
                spec = next((s for s in outline.sections if s.seq == rw.get("seq")), None)
                if target is None or spec is None:
                    continue
                remaining = self.deadline_s - (time.time() - t0)
                if remaining < 90:
                    self._emit(rid, "review", {"seq": rw.get("seq"), "status": "skipped",
                                               "reason": "剩余预算不足"})
                    break
                old_words = target.words or 0
                new = _with_timeout(
                    lambda: write_section(
                        outline, spec, template=template, prev=[], evidence=[],
                        universe=universe, llm=self.llm, log=self.log,
                        cancel_check=(cancel.is_set if cancel else None),
                        revision=rw, previous=target.content[:6000]),
                    int(min(_REVISE_TIMEOUT_MAX, remaining - 60)),
                    trace_entry=self._thread_trace_entry)
                if new is None:
                    self._emit(rid, "review", {"seq": rw.get("seq"), "status": "skipped",
                                               "reason": "返工超时/失败，保留原文"})
                    continue
                # 护栏：返工只改事实，不许把整节重写（字数漂移 >20% 即拒收）
                if new.status == M.SEC_OK and old_words and \
                        abs((new.words or 0) - old_words) > 0.2 * old_words:
                    self._emit(rid, "review", {"seq": rw.get("seq"), "status": "rejected",
                                               "reason": f"字数漂移 {(new.words or 0) - old_words:+d}"})
                    continue
                if new.status == M.SEC_OK:
                    results = [new if r.seq == new.seq else r for r in results]
                    self.store.save_section(rid, new)
                    self._emit(rid, "review", {"seq": new.seq, "status": "revised",
                                               "words": new.words})
            qa_after = run_qa(results, outline, cards, expected_ids=expected_ids)
            review_info["qa_after"] = qa_after
            qa = qa_after
            self._emit(rid, "qa", {"passed": qa["passed"],
                                   "conflicts": len(qa["conflicts"]), "round": 2})

            # 校验未过不翻 status：qa 结论单独挂黄条（partial 只表示有章节失败）
            try:
                self.store.save_qa(rid, {"qa": qa, "review": review_info})
            except Exception:
                logger.warning("QA 落盘失败 rid=%s", rid, exc_info=True)

        # ── 图表：候选图 → LLM 选 → 编译 → 每章最多 3 张（10 节报告目标 ≥10 张）──
        MAX_PER_SECTION = 3
        charts_by_seq: Dict[int, Dict[str, Any]] = {}
        if cards:
            from agents.deep_report.chart_candidates import build_candidates, select
            from agents.deep_report.chart_compiler import ChartSpecError, compile_spec

            # ① LLM 在取数时直接给出的 spec 最优先（数据最贴题）
            direct: List[Dict[str, Any]] = []
            for slot, card in cards.items():
                for spec in (card.get("charts") or []):
                    if isinstance(spec, dict) and spec.get("id"):
                        direct.append({"id": f"d_{spec.get('id')}", "slot": slot,
                                       "shape": "direct", "title": str(spec.get("title") or ""),
                                       "spec": spec, "priority": 9, "size": 0})
            cands = direct + build_candidates(cards)
            picked = select(cands, llm=self.llm, log=self.log) if cands else []
            by_id = {c["id"]: c for c in cands}
            key_to_seq: Dict[str, int] = {}
            for sec in chosen:
                for key in sec_keys.get(sec.seq) or sec.data_slots:
                    key_to_seq.setdefault(key, sec.seq)

            dropped = {"unmapped": 0, "compile_failed": 0, "section_full": 0}
            for cid in picked:
                c = by_id.get(cid) or {}
                seq = key_to_seq.get(c.get("slot"))
                if seq is None:
                    dropped["unmapped"] += 1
                    continue
                bucket = charts_by_seq.setdefault(seq, {})
                if len(bucket) >= MAX_PER_SECTION:   # 每章最多 3 张（原为 1 张，导致整篇只有 5 图）
                    dropped["section_full"] += 1
                    continue
                try:
                    bucket[cid] = compile_spec(c.get("spec") or {})
                except ChartSpecError as e:
                    dropped["compile_failed"] += 1
                    logger.warning("图表编译失败 %s: %s", cid, e)
            self._emit(rid, "charts", {"candidates": len(cands), "picked": len(picked),
                                       "placed": sum(len(v) for v in charts_by_seq.values()),
                                       "dropped": dropped})
        total_charts = sum(len(v) for v in charts_by_seq.values())

        # ── 汇编 ──
        self.store.update(rid, stage="assembling", progress=95)
        self._emit(rid, "phase", {"stage": "assembling", "progress": 95})
        self.store.assemble(rid, outline, results)

        words = sum(r.words or 0 for r in results if r.words is not None)
        status = M.STATUS_DONE if failed == 0 else M.STATUS_PARTIAL
        chosen_ids = {s.section_id for s in chosen}
        ok_sections = sum(1 for r in results
                          if r.status == M.SEC_OK and r.section_id in chosen_ids)
        # ⚠️ 顺序很重要：先 emit done、最后 finish。
        # 反过来（finish 先落终态、done 后发）时，/events 会在「无事件+终态」那拍收流，
        # done 只要慢于宽限（实测 >0.25s）就永远发不出去（评审 P1-1）。
        meta = self.store.read_meta(rid)
        meta.status = status
        meta.stage = "done"
        meta.progress = 100
        meta.words = words
        meta.charts = total_charts
        meta.sections_done = ok_sections
        meta.qa_json = json.dumps(qa, ensure_ascii=False)

        # ── 阅读页（先落盘，前端收到 done 打开时不会 404）──
        try:
            html_doc = build_report_html(meta, outline, results, charts=charts_by_seq)
            write_html(self.store.dir_of(rid) / "report.html", html_doc)
        except Exception:
            logger.warning("阅读页渲染失败 rid=%s", rid, exc_info=True)

        self._emit(rid, "done", {"status": status, "words": words,
                                 "sections_done": ok_sections, "charts": total_charts,
                                 "qa_passed": qa.get("passed")})

        meta = self.store.finish(rid, status, sections_done=ok_sections, progress=100,
                                 stage="done", words=words, charts=total_charts,
                                 qa_json=json.dumps(qa, ensure_ascii=False),
                                 error=f"{failed} 节失败或校验未过" if failed else "")
        return meta.to_dict()
