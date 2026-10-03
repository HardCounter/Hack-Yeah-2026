"""Pipeline trace logging shared by the three planes.

One small interface, three implementations:

    TerminalLogger  human-readable lines on stderr (stdout stays free for CLI output)
    FileLogger      JSON lines appended to a file
    NullLogger      discards everything (the default, so tests and libraries stay quiet)

Selection: `configure("terminal" | "file" | "null", path=...)`, or the environment variables
`CONTROL_LOG=terminal|file|null` and `CONTROL_LOG_FILE=<path>` (default `var/control-layer.log`).
Environment configuration also reaches subprocesses such as the OpenCode-facing gateway.

Callers pass identifiers, names and reason codes only. Values are reduced to bounded scalars here as a
second line of defence: never pass prompts, tool arguments, results or other raw content.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

MAX_VALUE_CHARS = 120
DEFAULT_LOG_FILE = "var/control-layer.log"
LAYERS = ("intercept", "persistence", "consume")


class PipelineLogger(Protocol):
    def log(self, layer: str, event: str, /, **fields: Any) -> None:
        """Record one pipeline event. `layer` is one of LAYERS; `event` is a dotted name."""
        ...


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "…"
    return f"<{type(value).__name__}>"  # never serialize structures: they may carry raw content


RESERVED = frozenset({"ts", "layer", "event"})


def _record(layer: str, event: str, fields: dict[str, Any]) -> dict[str, Any]:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "layer": layer, "event": event}
    for key, value in fields.items():
        if value is not None:
            record[f"{key}_" if key in RESERVED else key] = _safe(value)  # never overwrite the record header
    return record


class NullLogger:
    def log(self, layer: str, event: str, /, **fields: Any) -> None:
        pass


class TerminalLogger:
    _COLORS = {"intercept": "\033[36m", "persistence": "\033[35m", "consume": "\033[33m"}
    _RESET = "\033[0m"
    _BAD = {"BLOCK", "REQUIRE_APPROVAL", "nacked", "failed", "error"}

    def __init__(self, stream: TextIO | None = None, color: bool | None = None):
        self.stream = stream or sys.stderr
        self.color = self.stream.isatty() if color is None else color
        self._lock = threading.Lock()

    def log(self, layer: str, event: str, /, **fields: Any) -> None:
        rec = _record(layer, event, fields)
        stamp = rec.pop("ts")[11:23]
        rec.pop("layer"), rec.pop("event")
        details = " ".join(f"{k}={v}" for k, v in rec.items())
        tag = f"{layer:<11}"
        if self.color:
            tag = f"{self._COLORS.get(layer, '')}{tag}{self._RESET}"
            if any(str(v) in self._BAD for v in rec.values()) or event.endswith(("failed", "error")):
                event = f"\033[31m{event}{self._RESET}"
        with self._lock:
            print(f"{stamp} {tag} {event:<22} {details}", file=self.stream, flush=True)


class FileLogger:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, layer: str, event: str, /, **fields: Any) -> None:
        line = json.dumps(_record(layer, event, fields), sort_keys=True, ensure_ascii=False)
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as out:
                out.write(line + "\n")
        except OSError:
            pass  # tracing must never break the pipeline it observes


def make_logger(kind: str, path: str | os.PathLike[str] | None = None) -> PipelineLogger:
    if kind == "terminal":
        return TerminalLogger()
    if kind == "file":
        return FileLogger(path or os.environ.get("CONTROL_LOG_FILE") or DEFAULT_LOG_FILE)
    if kind in ("null", "", "none", "off"):
        return NullLogger()
    raise ValueError(f"unknown logger {kind!r}; use terminal, file or null")


_logger: PipelineLogger | None = None


def get_logger() -> PipelineLogger:
    """The process-wide logger, configured from CONTROL_LOG on first use."""
    global _logger
    if _logger is None:
        try:
            _logger = make_logger(os.environ.get("CONTROL_LOG", "null").strip().lower())
        except ValueError:
            _logger = NullLogger()
    return _logger


def set_logger(logger: PipelineLogger) -> PipelineLogger | None:
    """Install `logger` process-wide; returns the previous one (useful in tests)."""
    global _logger
    previous, _logger = _logger, logger
    return previous


def configure(kind: str, path: str | os.PathLike[str] | None = None) -> PipelineLogger:
    logger = make_logger(kind, path)
    set_logger(logger)
    return logger


__all__ = ["PipelineLogger", "NullLogger", "TerminalLogger", "FileLogger", "make_logger", "get_logger",
           "set_logger", "configure", "LAYERS"]
