"""通知层路由：/api/notification..."""
import asyncio
from functools import partial

from fastapi import APIRouter

from server.schemas import NotificationTestRequest, NotificationSendRequest

router = APIRouter()


@router.get("/api/notification/status")
async def notification_status():
    """返回所有通知渠道的配置和断路器状态"""
    from utils.notification import NotificationManager, init_notification_channels
    init_notification_channels()
    manager = NotificationManager()
    return manager.status()


@router.post("/api/notification/test")
async def test_notification(payload: NotificationTestRequest | None = None):
    """发送测试通知"""
    from utils.notification import send_notification, init_notification_channels
    init_notification_channels()
    channel = payload.channel if payload else None
    channels = [channel] if channel else None

    # 使用线程池执行同步操作，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None,
        partial(
            send_notification,
            title="🔔 选股雷达 - 通知测试",
            content="这是一条测试通知。如果您收到此消息，说明通知配置正确。",
            report_type="system",
            channels=channels,
            force=True,
        ),
    )
    return {"results": [r.to_dict() for r in results]}


@router.post("/api/notification/send")
async def send_notification_api(payload: NotificationSendRequest):
    """手动发送通知"""
    from utils.notification import send_notification, init_notification_channels
    init_notification_channels()

    # 使用线程池执行同步操作，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None,
        partial(
            send_notification,
            title=payload.title,
            content=payload.content,
            report_type=payload.type or "report",
            channels=payload.channels,
            force=payload.force or False,
            image_data=payload.image_data,
        ),
    )
    return {"results": [r.to_dict() for r in results]}
