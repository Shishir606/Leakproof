from __future__ import annotations

import logging
import re
from collections.abc import Mapping

_RECOVERY_PATH = re.compile(r"(?P<prefix>/(?:api/)?recover/)[^?\s\"]+")


def redact_recovery_target(value: str) -> str:
    """Remove signed recovery capabilities from request targets and log messages."""
    return _RECOVERY_PATH.sub(r"\g<prefix>[REDACTED]", value)


class RecoveryCapabilityFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_recovery_target(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_recovery_target(value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, Mapping):
            record.args = {
                key: redact_recovery_target(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def install_access_log_redaction() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RecoveryCapabilityFilter) for item in access_logger.filters):
        access_logger.addFilter(RecoveryCapabilityFilter())
