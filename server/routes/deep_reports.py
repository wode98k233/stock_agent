"""长报告 API：/api/deep-reports/*

- 任务独立于对话 turn（自己的线程 + 事件表），不占 `state.eventSource`；
- 事件从 DB 重放（`id` 游标），刷新/断线可续；
- 正文/阅读页直接读文件（`output/deep_reports/<rid>/`）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("radar.deep_report.api")

router = APIRouter(prefix="/api/deep-reports", tags=["deep-reports"])

SSE_POLL_SECONDS = 1.0
SSE_MAX_TICKS = 1800   # 兼容旧引用；实际上限见 _sse_max_ticks()（配置项可覆盖）
SSE_FINAL_GRACE_SECONDS = 0.2   # 终态保护：状态已落库、终态事件可能还差一次提交
SSE_HEARTBEAT_TICKS = 30       # 空闲时每 ~30s 一帧心跳（有事件时不需要，只兜代理断长连接）
TERMINAL_STATUSES = ("done", "partial", "failed", "cancelled")


def _sse_max_ticks() -> int:
    """兜底收流上限：配置项 DEEP_REPORT_SSE_MAX_SECONDS（默认 1800s）。"""
    try:
        from config import Config
        seconds = int(getattr(Config, "DEEP_REPORT_SSE_MAX_SECONDS", 1800))
    except Exception:  # noqa: BLE001
        seconds = 1800
    return max(1, int(seconds / SSE_POLL_SECONDS))


def _sse_error(message: str) -> str:
    """SSE error 事件（业务错误；客户端收到后应关闭连接，服务端随后结束流）。"""
    return "event: error\ndata: " + json.dumps({"message": message}, ensure_ascii=False) + "\n\n"


def _resume_cursor(request: Request, since_id: int) -> int:
    """断线续传游标：EventSource 重连自动带 Last-Event-ID 头，**优先于**查询参数 since_id。"""
    raw = (request.headers.get("last-event-id") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.debug("忽略不可解析的 Last-Event-ID: %r", raw)
    return max(0, int(since_id or 0))


class StartReq(BaseModel):
    universe: str = Field(..., min_length=1, max_length=64)
    request: str = Field("", max_length=2000)
    template_id: str = Field("deep_sector_finance", max_length=64)
    max_sections: Optional[int] = Field(None, ge=1, le=30)
    fetch: bool = False
    # 对话 id：只允许字母数字下划线连字符，避免拼进消息/卡片时被注入
    dialog_uuid: str = Field("", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


def _svc():
    from server import deep_report_service
    return deep_report_service


def _store():
    from agents.deep_report.store import DeepReportStore
    return DeepReportStore()


_RID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


def _safe_rid(rid: str) -> str:
    """rid 会拼进文件路径，必须白名单校验（防 ../ 路径穿越）。"""
    if not _RID_RE.fullmatch(str(rid or "")):
        raise HTTPException(status_code=400, detail="非法报告 id")
    return str(rid)


@router.post("")
async def create_report(req: StartReq, request: Request):
    # 无效模板在起任务前就 400，避免「任务凭空消失、GET 404、SSE 空转」
    try:
        from agents.deep_report.templates import load_template
        load_template(req.template_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"未知模板: {req.template_id}（{e}）")
    rid = _svc().start(universe=req.universe, user_request=req.request,
                       template_id=req.template_id, max_sections=req.max_sections,
                       fetch=req.fetch, dialog_uuid=req.dialog_uuid)
    if req.dialog_uuid:
        _announce_card(request, req, rid)
    return {"code": 0, "data": {"rid": rid}, "msg": "ok"}


def _announce_card(request: Request, req: StartReq, rid: str) -> None:
    """在对话里落一张深度报告卡片（哨兵块；状态由前端按 rid 轮询，不写死在消息里）。"""
    try:
        from server.deps import get_web_state
        web = get_web_state(request)
        payload = {"type": "deep_report", "rid": rid,
                   "universe": req.universe, "template_id": req.template_id}
        content = ("@@DEEP_REPORT_START@@"
                   + json.dumps(payload, ensure_ascii=False)
                   + "@@DEEP_REPORT_END@@")
        web.storage.create_message(dialog_uuid=req.dialog_uuid, role="assistant",
                                   content=content, status="done", mode="deep_report")
    except Exception:  # noqa: BLE001 —— 卡片失败不能影响报告本身
        logger.warning("深度报告卡片写入失败 rid=%s", rid, exc_info=True)


@router.get("")
async def list_reports(limit: int = Query(50, ge=1, le=200)):
    try:
        def _load():
            from agents.deep_report.db import get_db
            return get_db().list_reports(limit)
        rows = await asyncio.to_thread(_load)   # 同步 SQLite 不阻塞事件循环
        return {"code": 0, "data": rows, "msg": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"code": 1, "data": None, "msg": f"DB 不可用: {e}"}


class PlanReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@router.post("/plan")
async def plan_report(req: PlanReq):
    """「本轮类型=深度报告」时，从用户问题里抽出标的与模板（不建任务）。

    失败/超时一律降级：universe 兜底取问题前 20 字、模板取第一个可用模板，
    绝不让「自动判断」把用户卡在原地。
    """
    from agents.deep_report.templates import list_templates

    tpls = list_templates() or ["deep_sector_finance"]
    default_tpl = next((t for t in tpls if "sector" in t), tpls[0])
    prompt = (
        "你是投研助手的路由器。从用户问题里抽 JSON，只输出 JSON：\n"
        '{"universe": "标的/行业名（如 银行板块、贵州茅台）", '
        '"template_id": "从候选里选一个", "request": "原问题精简版"}'
        f"\n候选模板：{tpls}\n用户问题：{req.text}"
    )
    universe, tpl, request = req.text[:20], default_tpl, req.text
    fallback = True
    try:
        from agents.deep_report.fetch import call_with_timeout
        from utils.llm_factory import get_report_llm

        def _call():
            return get_report_llm().invoke([("user", prompt)])

        resp = call_with_timeout(_call, 15.0)
        content = getattr(resp, "content", None) or str(resp)
        m = re.search(r"\{[\s\S]*\}", str(content))
        if m:
            data = json.loads(m.group(0))
            u = str(data.get("universe") or "").strip()
            t = str(data.get("template_id") or "").strip()
            if u:
                universe = u[:64]
                fallback = False
            if t in tpls:
                tpl = t
            request = str(data.get("request") or req.text)[:2000]
    except Exception as e:  # noqa: BLE001 —— 路由失败必须降级，不能 500
        logger.info("深度报告路由降级: %s", e)
    return {"code": 0, "data": {"universe": universe, "template_id": tpl,
                                "request": request, "fallback": fallback}, "msg": "ok"}


@router.get("/templates")
async def list_report_templates():
    """可用模板（页面下拉用；不要在前端写死某一个行业）。"""
    from agents.deep_report.templates import list_templates, load_template

    data = []
    for tid in list_templates():
        try:
            data.append({"id": tid, "name": load_template(tid).get("name", tid)})
        except Exception:  # noqa: BLE001
            continue
    return {"code": 0, "data": data, "msg": "ok"}


@router.get("/{rid}")
async def get_report(rid: str):
    rid = _safe_rid(rid)
    # snapshot 内部是同步 SQLite 读 + time.sleep 重试；扔进线程池，避免阻塞事件循环
    svc = _svc()
    row = await asyncio.to_thread(svc.snapshot, rid)
    if not row:
        raise HTTPException(status_code=404, detail=f"报告不存在: {rid}")
    return {"code": 0, "data": row, "msg": "ok"}


@router.post("/{rid}/cancel")
async def cancel_report(rid: str):
    ok = _svc().cancel(_safe_rid(rid))
    return {"code": 0, "data": {"cancelled": ok}, "msg": "ok" if ok else "无运行中任务"}


@router.get("/{rid}/events")
async def stream_events(rid: str, request: Request, since_id: int = Query(0, ge=0)):
    rid = _safe_rid(rid)                    # 格式非法 → 400（不查 DB）
    cursor = _resume_cursor(request, since_id)
    from agents.deep_report.db import get_db
    db = get_db()

    async def _gen():
        # 未知 rid：立刻发一条 error 事件并结束，绝不按 30 分钟心跳空转。
        # （报告占位行在 service.start() 里已先落库，不存在「POST 后建行前」的合法 rid 404 窗口）
        # ⚠️ DeepReportDB 是同步 sqlite：所有查询都走 asyncio.to_thread，
        #    否则一次 2~3ms 的读（还带 ensure_schema）会卡住整个 event loop。
        try:
            row = await asyncio.to_thread(db.get_report, rid)
        except Exception as e:  # noqa: BLE001
            yield _sse_error(f"DB 读取失败: {e}")
            return
        if not row:
            yield _sse_error(f"报告不存在: {rid}")
            return

        last = cursor
        idle_ticks = 0
        # 重连补发：先把游标之后已有的事件全吐出来（含终态）
        for tick in range(_sse_max_ticks()):
            try:
                events = await asyncio.to_thread(db.events_since, rid, last, 200)
            except Exception as e:  # noqa: BLE001
                yield _sse_error(str(e))
                return

            if not events:
                try:
                    row = await asyncio.to_thread(db.get_report, rid)
                except Exception:  # noqa: BLE001 —— 瞬时读失败不能当成「报告不存在」
                    row = None
                if row and row.get("status") in TERMINAL_STATUSES:
                    # 状态可能先于终态事件落库（finish → append done），宽限一拍再确认，避免丢 done/error
                    await asyncio.sleep(SSE_FINAL_GRACE_SECONDS)
                    try:
                        events = await asyncio.to_thread(db.events_since, rid, last, 200)
                    except Exception as e:  # noqa: BLE001
                        yield _sse_error(str(e))
                        return
                    if not events:
                        return  # 事件发完 + 任务终态 → 正常收流，不傻等
            for ev in events:
                last = ev["id"]
                body = json.dumps({"id": ev["id"], "type": ev["type"], **ev["data"]},
                                  ensure_ascii=False, default=str)
                yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {body}\n\n"
                if ev["type"] in ("done", "error"):
                    return
            if events:
                idle_ticks = 0
                if len(events) >= 200:
                    continue                    # 还有积压：立刻再拉一批，不额外 sleep
            else:
                idle_ticks += 1
                # 无事件时才心跳：首拍立即发，长任务期间每 ~30s 一帧，保证代理不断流
                if idle_ticks == 1 or idle_ticks % SSE_HEARTBEAT_TICKS == 0:
                    yield "event: heartbeat\ndata: {}\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _report_dir(rid: str) -> Path:
    return _store().dir_of(rid)


@router.get("/{rid}/html")
async def report_html(rid: str):
    p = _report_dir(_safe_rid(rid)) / "report.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="阅读页尚未生成")
    return FileResponse(str(p), media_type="text/html")


@router.get("/{rid}/markdown")
async def report_markdown(rid: str):
    p = _report_dir(_safe_rid(rid)) / "report.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="正文尚未生成")
    # 以附件下载（Content-Disposition），否则浏览器会直接打开纯文本
    return FileResponse(str(p), media_type="text/markdown",
                        filename=f"{rid}.md")


@router.get("/{rid}/pdf")
async def report_pdf(rid: str, request: Request):
    """阅读页 → PDF（Playwright/Chromium 打印；失败时明确报 501，不返回空文件）。"""
    rid = _safe_rid(rid)
    p = _report_dir(rid) / "report.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="阅读页尚未生成")
    base = str(request.base_url).rstrip("/")
    try:
        pdf_bytes = await asyncio.to_thread(_render_pdf, f"{base}/api/deep-reports/{rid}/html", rid)
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF 导出失败 rid=%s: %s", rid, e, exc_info=True)
        raise HTTPException(status_code=501, detail=f"PDF 导出不可用（需要 Playwright/Chromium）: {e}")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{rid}.pdf"'})


def _render_pdf(url: str, rid: str) -> bytes:
    """用 Chromium 打印阅读页（同步 API 放在线程里跑，避免阻塞事件循环）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)          # 等 ECharts 画完
            page.emulate_media(media="print")
            return page.pdf(format="A4", print_background=True,
                            margin={"top": "14mm", "bottom": "14mm",
                                    "left": "12mm", "right": "12mm"})
        finally:
            browser.close()


@router.get("/{rid}/sections/{seq}")
async def get_section(rid: str, seq: int):
    d = _report_dir(_safe_rid(rid)) / "sections"
    if not d.exists():
        raise HTTPException(status_code=404, detail="章节尚未生成")
    for p in sorted(d.glob(f"{seq:02d}_*.md")):
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown")
    raise HTTPException(status_code=404, detail=f"章节不存在: {seq}")
