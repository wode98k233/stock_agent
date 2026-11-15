"""图片转文字（vision）：把用户上传的图转成结构化文字，供纯文本 agent 链路消费。"""
from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

from langchain_core.messages import HumanMessage

from server.uploads import read_image

logger = logging.getLogger(__name__)

# 转述是同步阻塞在「发送消息」路径上的，必须设硬超时：
# 视觉调用带思考时实测 20s 左右（简单图），大截图/详细清单会更久；超时降级为「识别失败」。
CAPTION_TIMEOUT_S = 90

CAPTION_PROMPT_TEMPLATE = (
    "你是一个「图像转文字」助手。用户会发来各种图片（行情截图、公告/研报、表格、聊天或网页截图、照片等），"
    "你要把图片转成**尽可能详尽、结构化、可直接被纯文本模型使用**的文字描述。\n"
    "\n"
    "【第一步：判断类型并抓重点】\n"
    "- 行情/K线/分时：标的与周期、价格与涨跌、均线排列、K线结构（高低点/通道/箱体）、"
    "图上所有标注（斐波那契通道/回撤线、字母、箭头、买卖点、百分比）、副图指标（MACD/KDJ/RSI/BOLL）、成交量；\n"
    "- 公告/研报/新闻：标题、发布方、日期、关键数字（营收/利润/目标价/评级）、结论；\n"
    "- 表格：表头、行列结构、关键数值与单位、排序/合计；\n"
    "- 聊天/网页/软件界面：页面标题、菜单与按钮、正文文字、状态提示；\n"
    "- 照片/其他：主体、场景、人物或物体、图中可见文字。\n"
    "\n"
    "【第二步：按下面分节输出，总长 ≤{max_chars} 字】\n"
    "【图类型】一句话\n"
    "【文字转写】尽量逐字转写图中所有可读文字（OCR），保留数字、单位、百分比、日期\n"
    "【结构与标注】布局与位置（上/下/左/右）、颜色编码（红涨绿跌等）、箭头/连线/高亮、图表元素\n"
    "【关键信息】这张图最重要的 3–6 个事实（数字、结论、异常）\n"
    "【一句话结论】\n"
    "\n"
    "要求：\n"
    "1) 看不清的写「看不清」，禁止编造数字或结论；\n"
    "2) 数字优先保留原样（不换算、不四舍五入）；\n"
    "3) 中文输出，专有名词保留原文。"
)


def build_caption_prompt() -> str:
    """按配置拼转述提示词（UPLOAD_CAPTION_MAX_CHARS 控制输出字数）。"""
    from config import Config
    max_chars = int(getattr(Config, "UPLOAD_CAPTION_MAX_CHARS", 500) or 500)
    return CAPTION_PROMPT_TEMPLATE.format(max_chars=max_chars)


def caption_bytes(raw: bytes, mime: str, timeout_s: float = CAPTION_TIMEOUT_S) -> str:
    """把图片字节转成文字描述；失败/超时返回空串（调用方决定降级方式）。"""
    if not raw:
        return ""
    from utils.llm_factory import get_llm

    b64 = base64.b64encode(raw).decode("ascii")
    content = [
        {"type": "text", "text": build_caption_prompt()},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]

    def _call() -> str:
        resp = get_llm().invoke([HumanMessage(content=content)])
        return str(getattr(resp, "content", "") or "").strip()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_call).result(timeout=timeout_s)
    except FutureTimeout:
        logger.warning("图片转述超时（>%.0fs），降级为识别失败", timeout_s)
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning("图片转述失败: %s", e)
        return ""
    finally:
        pool.shutdown(wait=False)


def caption_upload(att_id: str) -> Optional[str]:
    """按上传 id 取图并转述；文件不存在返回 None，识别失败返回空串。"""
    got = read_image(att_id)
    if not got:
        return None
    raw, mime = got
    return caption_bytes(raw, mime)
