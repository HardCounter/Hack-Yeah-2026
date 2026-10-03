"""Load and validate consume_plane.yaml (docs/consumer-plane.md section 11)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..model.outputs import ADJUSTMENT_ORDER


class ConfigError(ValueError):
    pass


@dataclass
class SourceConfig:
    type: str = "memory"                 # memory | jsonl_replay
    path: str | None = None
    batch_size: int = 64
    poll_timeout_s: float = 0.5


@dataclass
class TrajectoryConfig:
    type: str = "memory"                 # memory | jsonl
    path: str | None = None
    contracts_path: str | None = None


@dataclass
class FeedbackConfig:
    enabled: bool = True
    allow_agent_scope: bool = False
    max_ttl_s: int = 3600
    max_signals_per_session_per_minute: int = 6
    allowed_actions: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class PluginEntry:
    name: str
    handler: str | None = None           # "package.module:Class"; None = found in plugin_dirs
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConsumePlaneConfig:
    source: SourceConfig = field(default_factory=SourceConfig)
    trajectory: TrajectoryConfig = field(default_factory=TrajectoryConfig)
    partitions: int = 8
    plugin_timeout_s: float = 2.0
    max_attempts: int = 3
    retry_backoff_s: float = 1.0
    on_plugin_load_error: str = "fail"   # fail | skip
    plugin_dirs: list[str] = field(default_factory=list)
    ledger_path: str = ":memory:"
    sinks: list[dict[str, Any]] = field(default_factory=list)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    plugins: dict[str, PluginEntry] = field(default_factory=dict)


def _build(cls, data: Mapping[str, Any] | None, where: str):
    data = dict(data or {})
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    return cls(**data)


def _resolve(base: Path, value: str | None) -> str | None:
    if value is None or value == ":memory:":
        return value
    p = Path(value)
    return str(p if p.is_absolute() else base / p)


def parse_config(doc: Mapping[str, Any], base_dir: str | Path = ".") -> ConsumePlaneConfig:
    base = Path(base_dir)
    unknown_top = set(doc) - {"consume_plane", "plugins"}
    if unknown_top:
        raise ConfigError(f"top level: unknown keys {sorted(unknown_top)}")
    core = dict(doc.get("consume_plane") or {})
    cfg = _build(ConsumePlaneConfig, {k: v for k, v in core.items()
                                      if k not in ("source", "trajectory", "feedback")}, "consume_plane")
    cfg.source = _build(SourceConfig, core.get("source"), "consume_plane.source")
    cfg.trajectory = _build(TrajectoryConfig, core.get("trajectory"), "consume_plane.trajectory")
    cfg.feedback = _build(FeedbackConfig, core.get("feedback"), "consume_plane.feedback")

    if cfg.on_plugin_load_error not in ("fail", "skip"):
        raise ConfigError("consume_plane.on_plugin_load_error must be 'fail' or 'skip'")
    if cfg.partitions < 1 or cfg.max_attempts < 1:
        raise ConfigError("consume_plane.partitions and max_attempts must be >= 1")
    for plugin, actions in cfg.feedback.allowed_actions.items():
        bad = set(actions) - set(ADJUSTMENT_ORDER)
        if bad:
            raise ConfigError(f"consume_plane.feedback.allowed_actions.{plugin}: unknown actions {sorted(bad)}")

    plugins = {}
    for name, entry in (doc.get("plugins") or {}).items():
        plugins[name] = _build(PluginEntry, {"name": name, **(entry or {})}, f"plugins.{name}")
    cfg.plugins = plugins

    cfg.plugin_dirs = [_resolve(base, d) for d in cfg.plugin_dirs]
    cfg.ledger_path = _resolve(base, cfg.ledger_path)
    cfg.source.path = _resolve(base, cfg.source.path)
    cfg.trajectory.path = _resolve(base, cfg.trajectory.path)
    cfg.trajectory.contracts_path = _resolve(base, cfg.trajectory.contracts_path)
    cfg.sinks = [{**s, "path": _resolve(base, s["path"])} if "path" in s else s for s in cfg.sinks]
    return cfg


def load_config(path: str | Path) -> ConsumePlaneConfig:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, Mapping):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return parse_config(doc, path.parent)
