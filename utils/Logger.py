import logging
import os
from logging.handlers import TimedRotatingFileHandler
from contextvars import ContextVar
from datetime import datetime

# 每个请求的唯一 ID（async context-local）
_request_trace_id: ContextVar[str] = ContextVar("request_trace_id", default="")

LOG_DIR = "/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)

# 日志格式：带 trace_id 前缀
LOG_FORMAT = "%(asctime)s | %(levelname)s | [%(trace_id)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 配置 root logger
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# 避免重复添加 handler（热更新时可能重复调用）
if not _root_logger.handlers:
    # 控制台 handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    _root_logger.addHandler(console)

    # 每小时轮转文件 handler
    file_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, "chat2api.log"),
        when="H",          # 每小时
        interval=1,
        backupCount=0,     # 0 = 不删除旧文件，只轮转
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    _root_logger.addHandler(file_handler)


class TraceIdFilter(logging.Filter):
    """每个 LogRecord 自动注入当前请求的 trace_id"""
    def filter(self, record):
        record.trace_id = _request_trace_id.get() or "background"
        return True


# 给所有已有 handler 挂上 filter（后续新加的 handler 也会自动带上）
for h in _root_logger.handlers:
    # 避免重复挂filter（热更新时可能已挂）
    if not any(isinstance(f, TraceIdFilter) for f in h.filters):
        h.addFilter(TraceIdFilter())


def set_trace_id(tid: str):
    """在请求入口调用，设置当前上下文的 trace_id"""
    _request_trace_id.set(tid)


def get_trace_id() -> str:
    return _request_trace_id.get() or "background"


class Logger:
    @staticmethod
    def info(message):
        logging.info(str(message))

    @staticmethod
    def warning(message):
        logging.warning("\033[0;33m" + str(message) + "\033[0m")

    @staticmethod
    def error(message):
        logging.error("\033[0;31m" + "-" * 50 + "\n| " + str(message) + "\033[0m" + "\n" + "└" + "-" * 80)

    @staticmethod
    def debug(message):
        logging.debug("\033[0;37m" + str(message) + "\033[0m")


logger = Logger()
