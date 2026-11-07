"""Logging setup with a rotating file and a compact console format.

Every module uses ``get_logger(__name__)``.  The gateway itself runs behind a
terminal, so console lines stay short; the file gets the full picture.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

_CONFIGURED = False
CONSOLE_FORMAT = "%(asctime)s %(levelname).1s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

# Third-party noise that would drown the interesting lines.
NOISY = ("urllib3", "http.client", "asyncio")


def setup(level: str = "INFO", log_dir: str = "./logs", filename: str = "gateway.log") -> None:
    """Idempotent process-wide logging setup."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        return

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, filename),
                maxBytes=8 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
            root.addHandler(handler)
        except OSError as exc:  # read-only directory: console only
            root.warning("file logging disabled: %s", exc)

    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
