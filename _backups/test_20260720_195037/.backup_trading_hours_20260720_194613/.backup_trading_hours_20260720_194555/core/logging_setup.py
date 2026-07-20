"""Structured logging setup (loguru) shared by all modules."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from config.settings import get_settings, PROJECT_ROOT

_INITIALIZED = False


def setup_logging(name: str = "mems_sniper") -> None:
    """Configure loguru sinks: stderr (rich) + rotating file."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    settings = get_settings()
    logger.remove()
    # Console sink with colorized, compact format.
    logger.add(
        sys.stderr,
        level=settings.log_level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )
    # Rotating file sink.
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / f"{name}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
    )
    _INITIALIZED = True


__all__ = ["setup_logging", "logger"]
