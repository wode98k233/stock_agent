"""
生成交易日历数据脚本

从 exchange-calendars 提取 CN/HK/US 全年交易日，输出 JSON 文件。
用于离线备份、调试、或 exchange-calendars 不可用时的降级数据。

用法:
    python test/scripts/gen_trading_calendar.py              # 生成当年
    python test/scripts/gen_trading_calendar.py 2026         # 生成指定年份
    python test/scripts/gen_trading_calendar.py 2025 2027    # 生成年份范围
"""
import json
import sys
from datetime import datetime, date
from pathlib import Path


def generate_calendar_json(start_year: int, end_year: int) -> dict:
    """生成交易日历 JSON 数据。"""
    try:
        import exchange_calendars as xcals
    except ImportError:
        print("错误: exchange-calendars 未安装。运行: pip install exchange-calendars")
        sys.exit(1)

    exchanges = {
        "cn": "XSHG",
        "hk": "XHKG",
        "us": "XNYS",
    }

    result = {
        "_meta": {
            "generated_at": datetime.now().isoformat(),
            "start_year": start_year,
            "end_year": end_year,
            "source": "exchange-calendars",
        },
        "markets": {},
    }

    for market, ex_code in exchanges.items():
        cal = xcals.get_calendar(ex_code)
        sessions = []

        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                for day in range(1, 32):
                    try:
                        d = date(year, month, day)
                    except ValueError:
                        continue
                    try:
                        if cal.is_session(datetime(year, month, day)):
                            sessions.append(d.isoformat())
                    except Exception:
                        continue

        result["markets"][market] = {
            "exchange": ex_code,
            "sessions": sessions,
            "count": len(sessions),
        }

    return result


def main():
    args = sys.argv[1:]
    if not args:
        year = datetime.now().year
        start_year = end_year = year
    elif len(args) == 1:
        start_year = end_year = int(args[0])
    else:
        start_year, end_year = int(args[0]), int(args[1])

    print(f"生成 {start_year}-{end_year} 年交易日历...")
    data = generate_calendar_json(start_year, end_year)

    out_dir = Path(__file__).resolve().parent.parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "trading_calendar.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"已保存到: {out_file}")
    for mkt, info in data["markets"].items():
        print(f"  {mkt}: {info['count']} 个交易日")


if __name__ == "__main__":
    main()
