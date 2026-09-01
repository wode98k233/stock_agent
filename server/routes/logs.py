"""日志 × agent_trace 关联检索路由：/api/logs/correlate。

Phase 3（ADR-005）：输入 dialog_uuid / task_id / trace_run_id 任一，
返回对话级日志内容 + 关联 trace 步骤，供前端「关联检索面板」并排呈现与双向跳转。
不改动 tracing / logging 内部，只读现有 web_messages 关联键与日志文件。
"""
import logging
import os
import glob

from fastapi import APIRouter, Request

from server.deps import get_web_state
from utils.app_paths import get_logs_dir
from server.routes.traces import query_trace_steps

logger = logging.getLogger(__name__)
router = APIRouter()

# 单条关联日志最多回传行数，避免超大日志撑爆响应
_MAX_LOG_LINES = 2000


def _read_log_lines(log_file: str | None) -> list:
    """安全读取对话级日志文件内容（按行返回）。

    log_file 以 "logs/..." 形式相对项目根存储，需先去掉前缀再拼到 logs 目录，
    否则会变成 logs/logs/... 双重路径导致文件找不到（历史 bug，log_lines 恒为 0）。
    """
    if not log_file:
        return []
    base = os.path.normpath(get_logs_dir())
    rel = log_file
    if rel.startswith("logs/") or rel.startswith("logs\\"):
        rel = rel[len("logs/"):]
    full = os.path.normpath(os.path.join(get_logs_dir(), rel))
    # 路径穿越防护：仅允许读取 logs 目录内文件
    if full != base and not full.startswith(base + os.sep):
        logger.warning("[logs] 拒绝非法日志路径: %s", log_file)
        return []
    try:
        with open(full, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("[logs] 读取日志失败: %s", e)
        return []
    if len(lines) > _MAX_LOG_LINES:
        return lines[-_MAX_LOG_LINES:]
    return lines


def _read_system_logs(dialog_uuid: str | None) -> list:
    """按 dialog_uuid 检索系统级日志（radar-system.log 及其轮转备份）。

    仅检索 logs 目录下以 radar-system.log 开头的文件，避免路径穿越。
    返回包含该 dialog_uuid 的日志行（含轮转备份），最多 _MAX_LOG_LINES 行。
    """
    if not dialog_uuid:
        return []
    base = os.path.normpath(get_logs_dir())
    # 仅检索 logs 目录内、以 radar-system.log 开头的文件（含轮转 .1/.2）
    candidates = [os.path.join(base, "radar-system.log")]
    candidates += sorted(glob.glob(os.path.join(base, "radar-system.log.*")), reverse=True)
    needle = str(dialog_uuid)
    out = []
    for cand in candidates:
        full = os.path.normpath(cand)
        if not full.startswith(base + os.sep):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if needle in line:
                        out.append(line.rstrip("\n"))
                        if len(out) >= _MAX_LOG_LINES:
                            return out
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning("[logs] 读取系统日志失败 %s: %s", cand, e)
    return out


@router.get("/api/logs/correlate")
async def correlate(
    request: Request,
    q: str = "",
    dialog_uuid: str = "",
    web_dialog_uuid: str = "",
):
    """按 dialog_uuid / web_dialog_uuid / task_id / trace_run_id 任一检索关联日志。

    参数优先级：dialog_uuid > web_dialog_uuid > q（任一提供即可）。
    - 优先在 web_messages 反查对话行（含 trace_run_id / log_file）；
    - 即使 web_messages 查不到，只要系统日志(radar-system.log)含该 uuid，
      仍返回 system_log_lines（ADR-005 Phase 3 Tier 2：系统日志按 dialog_uuid 关联检索）。
    """
    key = (dialog_uuid or web_dialog_uuid or q).strip()
    web = get_web_state(request)
    row = web.storage.correlate(key) if key else None

    # 系统日志：无论是否查到对话行都尝试检索（dialog_uuid 可能只存在于系统日志）
    sys_uuid = (row.get("dialog_uuid") if row else None) or key
    system_log_lines = _read_system_logs(sys_uuid) if key else []

    trace_run_id = row.get("trace_run_id") if row else None
    steps = query_trace_steps(trace_run_id).get("items", []) if trace_run_id else []

    log_lines = _read_log_lines(row.get("log_file")) if row and row.get("log_file") else []

    found = bool(row) or bool(system_log_lines) or bool(steps)
    return {
        "found": found,
        "query": key,
        "dialog_uuid": (row.get("dialog_uuid") if row else None) or (key if system_log_lines else None),
        "task_id": row.get("task_id") if row else None,
        "log_file": row.get("log_file") if row else None,
        "trace_run_id": trace_run_id,
        "log_lines": log_lines,
        "trace_steps": steps,
        "system_log_lines": system_log_lines,
    }
