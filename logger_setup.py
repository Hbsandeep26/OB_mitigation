from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional


class MillisecondFormatter(logging.Formatter):
    """Formatter using YYYY-MM-DD HH:MM:SS.uuu timestamps."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt_value = datetime.fromtimestamp(record.created)
        if datefmt:
            return dt_value.strftime(datefmt)
        return dt_value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _clear_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _build_handler(path: str, level: int) -> logging.Handler:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        MillisecondFormatter(
            "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )
    )
    return handler


def setup_master_logger(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("master_engine")
    logger.setLevel(level)
    logger.propagate = False
    _clear_handlers(logger)
    logger.addHandler(_build_handler(os.path.join(log_dir, "master_engine.log"), level))

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(MillisecondFormatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(console)
    return logger


def setup_strategy_logger(
    strategy_name: str,
    log_dir: str = "logs",
    level: int = logging.INFO,
) -> logging.Logger:
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in strategy_name)
    logger = logging.getLogger(f"strategy.{safe_name}")
    logger.setLevel(level)
    logger.propagate = False
    _clear_handlers(logger)
    logger.addHandler(_build_handler(os.path.join(log_dir, f"strategy_{safe_name}.log"), level))
    return logger
