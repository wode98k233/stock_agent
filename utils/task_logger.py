"""数据采集任务日志记录器

独立于 agent/web 日志，记录在 logs/task/{日期}/ 目录下。
每个任务一个文件：logs/task/2026-06-07/16-49-{task_id}.log
记录任务全生命周期的详细信息，包括数据源调用、入库、错误堆栈等。
"""
import logging
import os
from datetime import datetime
from utils.app_paths import get_logs_dir


class TaskLogger:
    """任务日志记录器

    每个任务独立一个日志文件，按日期分文件夹。
    线程安全，可在后台线程中使用。
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        now = datetime.now()
        self._log_dir = os.path.join(get_logs_dir(), "task", now.strftime('%Y-%m-%d'))
        os.makedirs(self._log_dir, exist_ok=True)
        self._logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """为当前任务创建独立的 logger"""
        logger_name = f"radar.task.{self.task_id}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        # 保留独立 task 文件；同时向 root 传播，复用共享根 handler 兜底落盘（见 utils.logger.install_root_handler）
        logger.propagate = True

        # 避免重复添加 handler
        if logger.handlers:
            return logger

        # 文件名：时间-task_id.log（日期在文件夹名中）
        now = datetime.now()
        filename = f"{now.strftime('%H-%M')}-{self.task_id}.log"
        filepath = os.path.join(self._log_dir, filename)

        handler = logging.FileHandler(filepath, encoding='utf-8')
        handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-7s | %(message)s',
            datefmt='%H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def info(self, msg: str):
        self._logger.info(msg)

    def debug(self, msg: str):
        self._logger.debug(msg)

    def warning(self, msg: str):
        self._logger.warning(msg)

    def error(self, msg: str, exc_info: bool = False):
        self._logger.error(msg, exc_info=exc_info)

    def exception(self, msg: str):
        """记录异常（自动附加 traceback）"""
        self._logger.exception(msg)

    def source_try(self, source_name: str, method: str, target: str):
        """记录数据源尝试"""
        self.debug(f'[SOURCE] 尝试 {source_name}.{method}({target})')

    def source_ok(self, source_name: str, rows: int, elapsed_ms: float):
        """记录数据源成功"""
        self.info(f'[SOURCE] {source_name} 成功: {rows} 行, {elapsed_ms:.0f}ms')

    def source_fail(self, source_name: str, error: str):
        """记录数据源失败"""
        self.warning(f'[SOURCE] {source_name} 失败: {error}')

    def db_write(self, table: str, count: int):
        """记录数据库写入"""
        self.debug(f'[DB] 写入 {table}: {count} 条')

    def progress(self, current: int, total: int, detail: str = ''):
        """记录进度"""
        pct = (current / total * 100) if total > 0 else 0
        msg = f'[PROGRESS] {current}/{total} ({pct:.1f}%)'
        if detail:
            msg += f' — {detail}'
        self.info(msg)

    def task_start(self, task_type: str, config: dict):
        """记录任务开始"""
        self.info(f'{"="*60}')
        self.info(f'[START] 任务开始: {task_type}')
        self.info(f'[START] 配置: {config}')
        self.info(f'{"="*60}')

    def task_end(self, status: str, duration: float, stats: dict = None):
        """记录任务结束"""
        self.info(f'{"="*60}')
        msg = f'[END] 任务结束: {status}, 耗时 {duration:.1f}s'
        if stats:
            msg += f', 统计: {stats}'
        self.info(msg)
        self.info(f'{"="*60}')
