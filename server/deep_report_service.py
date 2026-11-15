"""长报告后台服务：起线程跑编排、维护取消标记。

为什么不用 TaskRuntime：长报告是**独立任务**（10–15 分钟），不应占用对话 turn 的
单例 SSE / 同 dialog 运行锁（评审 P0-3）。这里给每条报告一个线程 + 自己的事件表，
Web 侧用 `/api/deep-reports/{rid}/events` 单独订阅。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("radar.deep_report")

_CANCELS: Dict[str, threading.Event] = {}

# 上个进程遗留的「活跃」报告：进程被杀后不会再有线程推进它们，必须收尾，
# 否则任务列表永远停在「(未命名) writing 0/0」。
# 注：当前编排把 fetching/assembling 写在 stage 上，status 只有 outlining/writing；
# 这里仍按设计 §5.2 的完整状态集合过滤（多出来的值不会误伤任何正常记录）。
_ACTIVE_STATUSES = ("outlining", "fetching", "writing", "assembling")
# 模块导入时刻 ≈ 进程启动时刻（bootstrap 会显式传入自己的进程启动时间）。
_PROCESS_STARTED_AT = datetime.now()
_CLEANUP_LOCK = threading.Lock()
_CLEANUP_DONE = False


def _as_naive_local(value: Any) -> Optional[datetime]:
    """DB 时间串 / datetime → naive 本地时间；解析不了返回 None（不猜）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return dt


def cleanup_interrupted_reports(*, db=None, process_started_at: Any = None,
                                now: Optional[datetime] = None) -> int:
    """把**上个进程**遗留的活跃报告置为 partial + interrupted_at，返回清理条数。

    判据（缺一不可）：status 仍是活跃态、updated_at（缺失回退 created_at）
    早于 process_started_at。幂等：置 partial 后不再命中，且不会覆盖已有
    interrupted_at（第二次调用返回 0）。

    只改元数据库状态，不伪造 web_dialogs / 不写 trace。
    """
    started = _as_naive_local(process_started_at) or _PROCESS_STARTED_AT
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    if db is None:
        from agents.deep_report.db import get_db
        db = get_db()
    ctx = getattr(db, "_ctx", None)
    if ctx is None:
        logger.warning("长报告中断清理跳过：DB 缺少 _ctx（测试替身？）")
        return 0

    marks = ",".join("?" for _ in _ACTIVE_STATUSES)
    cleaned = 0
    with ctx() as conn:
        rows = conn.execute(
            f"SELECT rid, updated_at, created_at FROM deep_report "
            f"WHERE status IN ({marks})",
            _ACTIVE_STATUSES,
        ).fetchall()
        for row in rows:
            updated = _as_naive_local(row["updated_at"]) or _as_naive_local(row["created_at"])
            if updated is None or updated >= started:
                continue
            conn.execute(
                f"UPDATE deep_report SET status='partial', interrupted_at=? "
                f"WHERE rid=? AND status IN ({marks})",
                (stamp, row["rid"], *_ACTIVE_STATUSES),
            )
            cleaned += 1
    if cleaned:
        logger.info("长报告中断清理：%s 份活跃报告标记为 partial（interrupted_at=%s）",
                    cleaned, stamp)
    return cleaned


def cleanup_interrupted_reports_once(*, db=None, process_started_at: Any = None,
                                     now: Optional[datetime] = None) -> int:
    """进程内只执行一次的中断清理（启动钩子用；失败不标记 done，允许下次重试）。"""
    global _CLEANUP_DONE
    with _CLEANUP_LOCK:
        if _CLEANUP_DONE:
            return 0
        try:
            cleaned = cleanup_interrupted_reports(
                db=db, process_started_at=process_started_at, now=now)
        except Exception:
            logger.warning("长报告中断清理失败（忽略，下次启动再试）", exc_info=True)
            return 0
        _CLEANUP_DONE = True
        return cleaned


def cancel_event(rid: str) -> threading.Event:
    ev = _CANCELS.get(rid)
    if ev is None:
        ev = threading.Event()
        _CANCELS[rid] = ev
    return ev


def cancel(rid: str) -> bool:
    fate = _CANCELS.get(rid)
    if fate is None:
        return False
    fate.set()
    return True


def _store(db=None):
    from agents.deep_report.store import DeepReportStore
    return DeepReportStore(db=db)


def start(*, universe: str, user_request: str = "", template_id: str = "deep_sector_finance",
          max_sections: Optional[int] = None, fetch: bool = False,
          dialog_uuid: str = "") -> str:
    """起一条后台报告，返回 rid（立即返回，不阻塞请求）。"""
    from agents.deep_report.db import get_db
    from agents.deep_report.templates import load_template

    # 模板无效就在这里抛（路由会转 400）：否则线程里早于 store.create 失败，
    # 会留下一个「DB 无行、无 error 事件、GET 404」的孤儿 rid。
    load_template(template_id)

    db = get_db()
    store = _store(db)
    rid = store.new_rid()
    cancel_event(rid)   # 预建，避免取消请求早于线程启动

    # 先落一行占位（status=outlining）：POST 返回 rid 后前端会立刻 GET/开 SSE，
    # 而真正的 runtime._run 是后台线程里才建 meta —— 不占位就有「刚创建即 404」竞态。
    try:
        from agents.deep_report.models import ReportMeta
        store.create(ReportMeta(rid=rid, universe=universe, user_request=user_request,
                                template_id=template_id, variant="",
                                dialog_uuid=dialog_uuid,
                                status="outlining", stage="outline"))
    except Exception:  # noqa: BLE001 —— 占位失败不影响任务（runtime 还会再建一次）
        logger.warning("长报告占位行写入失败 rid=%s", rid, exc_info=True)

    def _run() -> None:
        from agents.deep_report.runtime import DeepReportRuntime
        llm = None
        executor = None
        log = None
        try:
            from utils.logger import RadarLogger
            from utils.llm_factory import get_llm, get_report_llm
            log = RadarLogger(logging.getLogger("deep_report"), rid)
            llm = get_report_llm()
            if fetch:
                from agents.deep_report.tool_profiles import DEFAULT_PROFILE, get_profile
                from agents.deep_report.tools_provider import load_tools
                from agents.subagent.executor import SubagentExecutor
                # ⚠️ 工具面按 profile 装载（方案 B：能力 18 + 研究类 12 = 30）。
                # 不能只装厂商 query 工具：能力模式要把真实技能工具交给子代理自己选。
                profile = get_profile(DEFAULT_PROFILE)
                tools = load_tools(whitelist=tuple(profile))
                logger.info("长报告工具面: profile=%s 期望 %d 个，实际装载 %d 个",
                            DEFAULT_PROFILE, len(profile), len(tools))
                if tools:
                    # 取数用报告模型：实测 agent 模型在多轮工具循环里更容易耗尽回合、返回空 items
                    executor = SubagentExecutor(llm=llm, tools=tools, log=log,
                                                deadline_s=120.0, max_default_turns=8)
        except Exception:
            logger.warning("长报告 LLM/工具初始化失败，继续离线模式", exc_info=True)

        runtime = DeepReportRuntime(store=store, llm=llm, log=log,
                                    executor=executor, fetch=bool(fetch and executor))
        try:
            runtime.run(universe=universe, user_request=user_request,
                        template_id=template_id, max_sections=max_sections,
                        cancel=cancel_event(rid), rid=rid, dialog_uuid=dialog_uuid)
        except Exception as e:  # noqa: BLE001 —— 线程里的异常必须落库，不能只打日志
            logger.exception("长报告执行失败 rid=%s", rid)
            _finish_failed(store, rid, universe, user_request, template_id,
                           error=f"{type(e).__name__}: {e}")
        finally:
            _CANCELS.pop(rid, None)

    t = threading.Thread(target=_run, name=f"deep-report-{rid}", daemon=True)
    t.start()
    return rid


def _finish_failed(store, rid: str, universe: str, user_request: str,
                   template_id: str, *, error: str) -> None:
    """把失败落库；若 meta 还没建出来（早失败），先补一行再收尾。"""
    try:
        store.finish(rid, "failed", error=error)
    except Exception:
        try:
            from agents.deep_report.models import ReportMeta
            store.create(ReportMeta(rid=rid, universe=universe,
                                    user_request=user_request,
                                    template_id=template_id, variant=""))
            store.finish(rid, "failed", error=error)
        except Exception:  # noqa: BLE001
            logger.warning("失败状态落库失败 rid=%s", rid, exc_info=True)
    try:
        store.append_event(rid, "error", {"message": error})
    except Exception:  # noqa: BLE001
        pass


def snapshot(rid: str) -> Optional[Dict[str, Any]]:
    """读快照（带一次重试：后台写事务可能短暂挡住读）。"""
    from agents.deep_report.db import get_db

    db = get_db()
    for attempt in range(3):
        try:
            return db.get_report(rid)
        except Exception:  # noqa: BLE001
            logger.warning("快照读取失败 rid=%s attempt=%s", rid, attempt + 1, exc_info=True)
            time.sleep(0.2)
    return None
