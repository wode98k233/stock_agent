"""CLI 日志工具函数

从日志目录扫描和提取日志信息，供 status.py 和 logs_cmd.py 共用。
"""
import os
import datetime


def scan_recent_logs(limit: int = 3) -> list[dict]:
    """扫描最近的日志文件，返回 [{uuid, query, time, path, size}] 列表"""
    from utils.app_paths import get_logs_dir

    logs_dir = get_logs_dir()
    if not os.path.isdir(logs_dir):
        return []

    files = []
    for f in os.listdir(logs_dir):
        if f.endswith(".log"):
            path = os.path.join(logs_dir, f)
            mtime = os.path.getmtime(path)
            uuid = f.replace(".log", "")
            files.append((mtime, uuid, path))

    files.sort(reverse=True)
    results = []
    for _, uuid, path in files[:limit]:
        query = extract_query_from_log(path)
        mtime_str = datetime.datetime.fromtimestamp(
            os.path.getmtime(path)
        ).strftime("%m-%d %H:%M")
        size = f"{os.path.getsize(path) / 1024:.1f}KB"
        results.append({"uuid": uuid, "query": query, "time": mtime_str, "path": path, "size": size})

    return results


def extract_query_from_log(path: str) -> str:
    """从日志文件中提取用户问题"""
    query = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for marker in ("问题:", "问题："):
                    idx = line.find(marker)
                    if idx >= 0:
                        query = line[idx + len(marker):].strip()
                        break
                if query:
                    break
    except Exception:
        pass
    return query
