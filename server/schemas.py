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


class ClarifyDecisionRequest(BaseModel):
    """澄清反问：用户作答（answers: {question_id: 文本}）、跳过部分题（skipped: [id]）
    或选择重新描述（rejected）。"""
    answers: dict | None = None
    skipped: list[str] | None = None
    rejected: bool = False


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


class AddMonitorRuleRequest(BaseModel):
    """新建盯盘规则。"""
    stock_code: str
    stock_name: str = ""
    rule_type: str  # price_above/price_below/pct_above/pct_below/pct_change
    threshold: float
    once_only: bool = True
    cooldown_s: int = 1800
    remark: str = ""


class PatchMonitorRuleRequest(BaseModel):
    """更新盯盘规则（全部可选，只改传入字段）。"""
    threshold: float | None = None
    enabled: bool | None = None
    once_only: bool | None = None
    cooldown_s: int | None = None
    remark: str | None = None
    stock_name: str | None = None


class MarkMonitorAlertsReadRequest(BaseModel):
    """标记告警已读：按 ids 或全部。"""
    ids: list[int] = []
    all: bool = False


class PortfolioTradeRequest(BaseModel):
    """手动记一笔持仓交易。"""
    stock_code: str
    stock_name: str = ""
    side: str  # buy / sell
    qty: float
    price: float
    trade_date: str  # YYYY-MM-DD
    market: str = "cn"
    fee: float = 0.0
    remark: str = ""


class PortfolioFromSignalRequest(BaseModel):
    """从 AI 信号一键记入持仓。"""
    signal_id: int
    qty: float
    price: float | None = None  # 前端预填信号 entry_price；无则要求用户提供


class EastmoneyPreviewRequest(BaseModel):
    """东财持仓同步：粘贴报文解析+预览（不入库）。"""
    paste_text: str


class EastmoneyImportRequest(BaseModel):
    """东财持仓同步：确认导入（preview 返回的 snapshot + diff 原样回传）。"""
    snapshot: dict
    diff: dict | None = None


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
