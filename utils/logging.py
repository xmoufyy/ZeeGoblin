"""Logging setup: console + rotating file, with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
# Discord bot tokens: three dot-separated base64url segments.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")


class RedactingFilter(logging.Filter):
    """Removes known secrets and anything shaped like a Discord token from log records."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 8]

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return _TOKEN_PATTERN.sub("[REDACTED-TOKEN]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call; leave record untouched
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def configure_logging(level: str, log_dir: Path, secrets: list[str] | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = RedactingFilter(secrets)
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "zeegoblin.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # discord.py's HTTP layer is chatty at DEBUG; keep it at INFO unless explicitly debugging.
    logging.getLogger("discord.http").setLevel(max(logging.INFO, root.level))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
