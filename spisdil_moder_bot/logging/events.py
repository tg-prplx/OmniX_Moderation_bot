from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Level colors
    DEBUG = "\033[36m"      # Cyan
    INFO = "\033[32m"       # Green
    WARNING = "\033[33m"    # Yellow
    ERROR = "\033[31m"      # Red
    CRITICAL = "\033[35m"   # Magenta

    # Component colors
    TIMESTAMP = "\033[90m"  # Dark gray
    EVENT = "\033[96m"      # Bright cyan
    KEY = "\033[94m"        # Blue
    VALUE = "\033[37m"      # White
    NUMBER = "\033[93m"     # Bright yellow
    STRING = "\033[92m"     # Bright green


def _colorize_value(value: Any) -> str:
    """Colorize value based on type."""
    if isinstance(value, bool):
        return f"{Colors.NUMBER}{value}{Colors.RESET}"
    elif isinstance(value, (int, float)):
        return f"{Colors.NUMBER}{value}{Colors.RESET}"
    elif isinstance(value, str):
        return f"{Colors.STRING}{value}{Colors.RESET}"
    elif value is None:
        return f"{Colors.DIM}None{Colors.RESET}"
    else:
        return f"{Colors.VALUE}{value}{Colors.RESET}"


def _format_key_value(key: str, value: Any) -> str:
    """Format key-value pair with colors."""
    return f"{Colors.KEY}{key}{Colors.RESET}={_colorize_value(value)}"


class ColoredConsoleRenderer:
    """Custom structlog renderer with ANSI colors and human-readable format."""

    def __init__(self, colored: bool = True):
        self.colored = colored and sys.stdout.isatty()

    def __call__(self, logger: Any, name: str, event_dict: dict) -> str:
        if not self.colored:
            # Fallback to simple format if colors disabled
            return structlog.processors.JSONRenderer()(logger, name, event_dict)

        # Extract standard fields
        timestamp = event_dict.pop("timestamp", "")
        level = event_dict.pop("level", "info").upper()
        event = event_dict.pop("event", "")

        # Color level based on severity
        level_colors = {
            "DEBUG": Colors.DEBUG,
            "INFO": Colors.INFO,
            "WARNING": Colors.WARNING,
            "ERROR": Colors.ERROR,
            "CRITICAL": Colors.CRITICAL,
        }
        level_color = level_colors.get(level, Colors.INFO)

        # Build the log line
        parts = []

        # Timestamp
        if timestamp:
            parts.append(f"{Colors.TIMESTAMP}[{timestamp}]{Colors.RESET}")

        # Level with color and padding
        parts.append(f"{level_color}{Colors.BOLD}{level:8}{Colors.RESET}")

        # Event name
        parts.append(f"{Colors.EVENT}{event}{Colors.RESET}")

        # Remaining key-value pairs
        if event_dict:
            kv_pairs = [_format_key_value(k, v) for k, v in event_dict.items()]
            parts.append(f"{Colors.DIM}|{Colors.RESET} " + f" {Colors.DIM}|{Colors.RESET} ".join(kv_pairs))

        return " ".join(parts)


def setup_logging(level: int = logging.INFO, use_json: bool = False) -> None:
    """
    Setup structured logging with colored console output.

    Args:
        level: Logging level (default: INFO)
        use_json: If True, use JSON format instead of colored output (default: False)
    """
    # Choose renderer based on preference
    if use_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = ColoredConsoleRenderer(colored=True)

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    # Setup stdlib logging with custom formatter for aiogram/httpx
    class ColoredFormatter(logging.Formatter):
        """Colored formatter for stdlib loggers."""

        LEVEL_COLORS = {
            logging.DEBUG: Colors.DEBUG,
            logging.INFO: Colors.INFO,
            logging.WARNING: Colors.WARNING,
            logging.ERROR: Colors.ERROR,
            logging.CRITICAL: Colors.CRITICAL,
        }

        def format(self, record: logging.LogRecord) -> str:
            if not sys.stdout.isatty():
                return super().format(record)

            level_color = self.LEVEL_COLORS.get(record.levelno, Colors.INFO)
            timestamp = self.formatTime(record, "%H:%M:%S")

            return (
                f"{Colors.TIMESTAMP}[{timestamp}]{Colors.RESET} "
                f"{level_color}{Colors.BOLD}{record.levelname:8}{Colors.RESET} "
                f"{Colors.DIM}{record.name}{Colors.RESET} "
                f"{Colors.VALUE}{record.getMessage()}{Colors.RESET}"
            )

    # Configure root logger
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter())

    logging.basicConfig(
        level=level,
        handlers=[handler],
        force=True,
    )

    # Set levels for noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


def log_event(event: str, **kwargs: Any) -> None:
    structlog.get_logger().info(event, **kwargs)


def log_error(event: str, **kwargs: Any) -> None:
    structlog.get_logger().error(event, **kwargs)
