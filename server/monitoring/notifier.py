"""告警通知包装：统一文案模板，复用 utils/notification 全套能力。

只做编程调用（send_notification 快捷函数），降噪/静默时段/渠道熔断
全部由 NoiseController 与渠道自身处理，不重复造轮子。
"""
import logging

from utils.notification import send_notification

logger = logging.getLogger(__name__)


class AlertNotifier:
    """盯盘告警推送器。"""

    def push(self, alert: dict) -> str:
        """推送单条告警。

        Returns:
            成功推送的渠道名（逗号分隔）；空串 = 未开启通知或全部失败。
            NOTIFICATION_ENABLED=false 时 send_notification 静默返回 []，
            告警仍会落库，仅不推送。
        """
        title = f"盯盘提醒: {alert['rule_desc']}"
        pct = alert.get("pct")
        pct_text = f"{pct:+.2f}%" if pct is not None else "无数据"
        content = (
            f"**{alert['rule_desc']}**\n\n"
            f"- 现价：{alert.get('price')}\n"
            f"- 涨跌幅：{pct_text}\n"
            f"- 股票：{alert['stock_name']}（{alert['stock_code']}）"
        )
        try:
            results = send_notification(title, content, report_type="alert")
        except Exception as e:
            logger.warning("[盯盘] 告警推送失败（不影响落库）: %s", e)
            return ""
        return ",".join(r.channel for r in results if getattr(r, "success", False))
