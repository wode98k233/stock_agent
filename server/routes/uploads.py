"""对话图片上传路由：POST /api/uploads（base64 JSON）、GET /api/uploads/{id}。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from server.uploads import read_image, save_image

router = APIRouter(tags=["uploads"])


class UploadRequest(BaseModel):
    name: str = ""
    mime: str = ""
    data: str = ""


@router.post("/api/uploads")
async def upload_image(payload: UploadRequest):
    try:
        meta = save_image(payload.data, payload.mime, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"code": 0, "data": meta, "msg": "ok"}


@router.get("/api/uploads/{att_id}")
async def get_image(att_id: str):
    got = read_image(att_id)
    if not got:
        raise HTTPException(status_code=404, detail="图片不存在或已过期")
    raw, mime = got
    return Response(content=raw, media_type=mime,
                    headers={"Cache-Control": "public, max-age=86400"})
