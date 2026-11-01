"""Structured JSON logging with PII masking.

Driver license numbers, SSNs/FEINs and dates of birth must not reach log files.
"""
import json
import logging
import re
import sys
from datetime import datetime, timezone

_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b\d{2}-\d{7}\b"), "[FEIN]"),
    # License numbers: a letter followed by 7+ digits (CA/NJ style), optionally spaced
    (re.compile(r"\b[A-Z]\d{4}[\s-]?\d{3,5}[\s-]?\d{0,5}\b"), "[DL#]"),
    # Any full date: a DOB can't be told apart from other dates in free text, and drivers born 2000+ exist
    (re.compile(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])[/-]((19|20)\d{2})\b"), "[DATE]"),
]


def mask_pii(text: str) -> str:
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_pii(record.getMessage()),
        }
        for key, value in getattr(record, "ctx", {}).items():
            payload[key] = mask_pii(str(value)) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = mask_pii(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("pdfminer", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.LoggerAdapter:
    return _CtxAdapter(logging.getLogger(name), {})


class _CtxAdapter(logging.LoggerAdapter):
    """logger.info("msg", submission_id=..., doc_id=...) -> structured fields."""

    def log(self, level, msg, *args, **kwargs):
        ctx = {k: kwargs.pop(k) for k in list(kwargs) if k not in ("exc_info", "stack_info", "stacklevel", "extra")}
        kwargs["extra"] = {"ctx": ctx}
        if self.isEnabledFor(level):
            self.logger.log(level, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        kwargs["exc_info"] = True
        self.log(logging.ERROR, msg, *args, **kwargs)
