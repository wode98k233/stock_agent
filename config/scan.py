"""后台巡检与市场监控（由 config.py 拆分生成，逐字迁移勿手改）。"""

import os  # noqa: F401


class _ScanMixin:
    MARKET_MONITOR_ENABLED = os.getenv("MARKET_MONITOR_ENABLED", "false").lower() == "true"

    MARKET_MONITOR_INTERVAL_S = int(os.getenv("MARKET_MONITOR_INTERVAL_S", "60"))

    # 市场情绪面板（方案 C：右栏温度计 + 异动快报）

    MARKET_DASHBOARD_ENABLED = os.getenv("MARKET_DASHBOARD_ENABLED", "true").lower() == "true"

    MARKET_DASHBOARD_REFRESH_S = int(os.getenv("MARKET_DASHBOARD_REFRESH_S", "10"))

    MARKET_DASHBOARD_ALERT_LIMIT = int(os.getenv("MARKET_DASHBOARD_ALERT_LIMIT", "30"))

    MARKET_DASHBOARD_NEWS_LIMIT = int(os.getenv("MARKET_DASHBOARD_NEWS_LIMIT", "20"))

    # 回测体检：最低数据覆盖率，低于该值判定 health=data_gap（提示补数）

    BACKGROUND_SCANS_ENABLED = os.getenv("BACKGROUND_SCANS_ENABLED", "true").lower() == "true"

    SCAN_STORAGE_ENABLED = os.getenv("SCAN_STORAGE_ENABLED", "true").lower() == "true"

    SCAN_STORAGE_FIRST_DELAY_S = int(os.getenv("SCAN_STORAGE_FIRST_DELAY_S", "45"))

    SCAN_STORAGE_INTERVAL_MINUTES = int(os.getenv("SCAN_STORAGE_INTERVAL_MINUTES", "720"))

    SCAN_THESIS_ENABLED = os.getenv("SCAN_THESIS_ENABLED", "true").lower() == "true"

    SCAN_THESIS_FIRST_DELAY_S = int(os.getenv("SCAN_THESIS_FIRST_DELAY_S", "90"))

    SCAN_THESIS_INTERVAL_MINUTES = int(os.getenv("SCAN_THESIS_INTERVAL_MINUTES", "240"))

    SCAN_CROWDING_ENABLED = os.getenv("SCAN_CROWDING_ENABLED", "true").lower() == "true"

    SCAN_CROWDING_FIRST_DELAY_S = int(os.getenv("SCAN_CROWDING_FIRST_DELAY_S", "150"))

    SCAN_CROWDING_INTERVAL_MINUTES = int(os.getenv("SCAN_CROWDING_INTERVAL_MINUTES", "1440"))
    # 短线情绪预热：启动后自动拉一次当日四池/天梯并固化快照（hithink 限频敏感，放最后错峰）

    SCAN_SHORTLINE_ENABLED = os.getenv("SCAN_SHORTLINE_ENABLED", "true").lower() == "true"

    SCAN_SHORTLINE_FIRST_DELAY_S = int(os.getenv("SCAN_SHORTLINE_FIRST_DELAY_S", "180"))

    SCAN_SHORTLINE_INTERVAL_MINUTES = int(os.getenv("SCAN_SHORTLINE_INTERVAL_MINUTES", "60"))

    SCAN_KLINE_ENABLED = os.getenv("SCAN_KLINE_ENABLED", "true").lower() == "true"

    SCAN_KLINE_FIRST_DELAY_S = int(os.getenv("SCAN_KLINE_FIRST_DELAY_S", "240"))

    SCAN_KLINE_INTERVAL_MINUTES = int(os.getenv("SCAN_KLINE_INTERVAL_MINUTES", "60"))

    SCAN_KLINE_BUDGET_S = int(os.getenv("SCAN_KLINE_BUDGET_S", "120"))

    SCAN_KLINE_MAX_CODES = int(os.getenv("SCAN_KLINE_MAX_CODES", "200"))

    # 全市场成交额日终补录：收盘后把当日成交额按收盘真值写入 market_snapshot 短历史，
    # 修正 roll_prev_amount 在盘中记录的半日值（10-16 实测记成 9147 亿 / 实际约 1.57 万亿）。
    # 默认关闭 —— 这是手动工具：`python scripts/ops/refresh_background_data.py
    # --only market_amount_refresh`；非交易日与收盘前会自动跳过。

    SCAN_MARKET_AMOUNT_ENABLED = os.getenv("SCAN_MARKET_AMOUNT_ENABLED", "false").lower() == "true"

    SCAN_MARKET_AMOUNT_FIRST_DELAY_S = int(os.getenv("SCAN_MARKET_AMOUNT_FIRST_DELAY_S", "3600"))

    SCAN_MARKET_AMOUNT_INTERVAL_MINUTES = int(os.getenv("SCAN_MARKET_AMOUNT_INTERVAL_MINUTES", "1440"))

    # 持仓账本 agent 注入（关键词门控 + 无持仓静默跳过；关闭即回到现状）

