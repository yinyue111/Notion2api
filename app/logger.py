import logging
import json
import time
from datetime import datetime

class JsonFormatter(logging.Formatter):
    """自定义 JSON 格式化器"""
    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        
        # 提取自定义 extra 字段
        if hasattr(record, "request_info"):
            log_record.update(record.request_info)
            
        # 如果有异常，记录 trace
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_record, ensure_ascii=False)

def _add_file_handler(logger) -> None:
    """Persistent copy of the JSON log (docker logs are lost on every recreate):
    NOTION_LOG_FILE (default /app/logs/notion2api.log; empty/off = disabled), rotated at
    NOTION_LOG_MAX_MB (default 20) with NOTION_LOG_BACKUPS (default 5) old files kept."""
    import os
    path = os.getenv("NOTION_LOG_FILE", "/app/logs/notion2api.log").strip()
    if not path or path.lower() in ("0", "off", "false", "no"):
        return
    try:
        from logging.handlers import RotatingFileHandler
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            return  # log dir not mounted (tests / local runs)
        mb = float(os.getenv("NOTION_LOG_MAX_MB", "20") or 20)
        fh = RotatingFileHandler(path, maxBytes=int(mb * 1024 * 1024),
                                 backupCount=int(os.getenv("NOTION_LOG_BACKUPS", "5") or 5), encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
    except Exception:
        pass  # never let logging setup break the service


def setup_logger(name="notion_opus"):
    """配置并返回单例全局 logger"""
    logger = logging.getLogger(name)
    
    # 防止重复添加 handler
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(JsonFormatter())
        
        logger.addHandler(console_handler)
        _add_file_handler(logger)
        
    return logger

# 全局单例 logger 实例
logger = setup_logger()
