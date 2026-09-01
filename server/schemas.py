"""Web API 请求/响应 Pydantic 模型。"""
from typing import Any
from pydantic import BaseModel


class CreateDialogRequest(BaseModel):
    title: str | None = None
    mode: str | None = None


class CreateMessageRequest(BaseModel):
    content: str
    mode: str | None = None


class SaveConfigRequest(BaseModel):
    changes: dict


class ApplyConfigRequest(BaseModel):
    changes: dict


class BudgetDecisionRequest(BaseModel):
    decision: str


class NotificationTestRequest(BaseModel):
    channel: str | None = None  # 指定渠道, None=全部


class NotificationSendRequest(BaseModel):
    title: str
    content: str
    type: str | None = "report"       # report / alert / system
    channels: list[str] | None = None  # 指定渠道, None=全部
    force: bool | None = False         # 跳过降噪
    image_data: str | None = None      # 图片 base64 数据


class ReportTemplateSaveRequest(BaseModel):
    template: dict[str, Any]


class ReportTemplateToggleRequest(BaseModel):
    enabled: bool


class ReportTemplateImportRequest(BaseModel):
    template: dict[str, Any]


class SkillEnabledRequest(BaseModel):
    enabled: bool


class SkillImportRequest(BaseModel):
    """导入请求（暂用 zip_path，后续扩展为 file upload）"""
    zip_path: str


class AddWatchlistRequest(BaseModel):
    stock_code: str
    stock_name: str = ""
    market: str = "cn"
    tags: str = "自选股"


class QuotesRequest(BaseModel):
    symbols: list[str]
    source: str = "auto"


class KlineSyncRequest(BaseModel):
    code: str
    days: int = 250
    source: str = "auto"


class RouteRulesSaveRequest(BaseModel):
    keywords: list[list]  # [[keyword, weight], ...]


class RouteTestRequest(BaseModel):
    text: str


class DashboardSaveRequest(BaseModel):
    definition: dict[str, Any]


class DashboardImportRequest(BaseModel):
    definition: dict[str, Any]


class DashboardAssociationsSaveRequest(BaseModel):
    data: dict[str, Any]
