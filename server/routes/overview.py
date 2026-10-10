"""全局总览 API：SSE 事件流 + JSON 快照（装配层，编排在 overview_service）。"""
import asyncio
import logging
import threading
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server.deps import get_web_state
from server.overview_service import load_snapshot, run_stream
from server.runtime.event_stream import EventStreamManager

logger = logging.getLogger("radar.overview")

router = APIRouter(prefix="/api/overview", tags=["overview"])
_stream = EventStreamManager()


def _enabled() -> bool:
    from config import Config
    return bool(getattr(Config, "OVERVIEW_ENABLED", True))


def _split_blocks(blocks: str | None) -> list[str] | None:
    if not blocks:
        return None
    return [b.strip() for b in blocks.split(",") if b.strip()]


def _build_ctx(request: Request | None, force: bool) -> dict:
    web = get_web_state(request) if request is not None else None
    skill_register = None
    if web is not None:
        try:
            skill_register = web.agent_context.skill_register
        except Exception:
            skill_register = None
    return {"web_storage": web.storage if web is not None else None,
            "skill_register": skill_register, "force": force}


@router.get("/stream")
async def stream(request: Request, blocks: str | None = None, force: int = 0):
    if not _enabled():
        async def _disabled():
            yield 'event: final\ndata: {"total":0,"ok":0,"failed":0,"enabled":false}\n\n'
        return StreamingResponse(_disabled(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})
    _stream.set_event_loop()
    rid = uuid4().hex
    _stream.create_queue(rid)
    ctx = _build_ctx(request, bool(force))

    def _worker():
        try:
            run_stream(ctx, blocks=_split_blocks(blocks), force=bool(force),
                       emit=lambda payload: _stream.put_event(rid, payload))
        except Exception as e:
            logger.exception("[overview] 事件流编排异常: %s", e)
            _stream.put_event(rid, {"event": "final", "data": {
                "total": 0, "ok": 0, "failed": 0, "error": str(e)}})
        finally:
            _stream.schedule_cleanup(rid, 60)

    threading.Thread(target=_worker, daemon=True).start()

    async def _events():
        async for payload in _stream.iter_events(rid):
            yield _stream.format_sse(payload)

    return StreamingResponse(
        _events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/snapshot")
async def snapshot(request: Request, blocks: str | None = None, force: int = 0):
    if not _enabled():
        return {"code": 0, "data": {"enabled": False, "blocks": {}}, "msg": "ok"}
    ctx = _build_ctx(request, bool(force))
    data = await asyncio.to_thread(load_snapshot, ctx, _split_blocks(blocks), bool(force))
    return {"code": 0, "data": {"enabled": True, **data}, "msg": "ok"}
