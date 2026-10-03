"""Build a ConsumerManager from configuration. Embedders may pass their own adapters."""
from __future__ import annotations

from typing import Iterable, Sequence

from ..adapters.jsonl import JsonlReplaySource, JsonlSink, jsonl_trajectory_reader
from ..adapters.memory import MemoryEventSource, MemoryFeedbackChannel, MemorySink, MemoryTrajectoryReader
from ..ports.event_source import EventSource
from ..ports.feedback import FeedbackChannel
from ..ports.sinks import FindingSink
from ..ports.trajectory import TrajectoryReader
from .config import ConfigError, ConsumePlaneConfig
from .feedback import FeedbackController
from .ledger import Ledger
from .manager import ConsumerManager
from .registry import load_registry


def make_source(cfg: ConsumePlaneConfig) -> EventSource:
    if cfg.source.type == "memory":
        return MemoryEventSource()
    if cfg.source.type == "jsonl_replay":
        if not cfg.source.path:
            raise ConfigError("consume_plane.source.path is required for jsonl_replay")
        return JsonlReplaySource(cfg.source.path)
    raise ConfigError(f"consume_plane.source.type: unsupported {cfg.source.type!r}")


def make_reader(cfg: ConsumePlaneConfig) -> TrajectoryReader:
    if cfg.trajectory.type == "memory":
        return MemoryTrajectoryReader()
    if cfg.trajectory.type == "jsonl":
        if not cfg.trajectory.path:
            raise ConfigError("consume_plane.trajectory.path is required for jsonl")
        return jsonl_trajectory_reader(cfg.trajectory.path, cfg.trajectory.contracts_path)
    raise ConfigError(f"consume_plane.trajectory.type: unsupported {cfg.trajectory.type!r}")


def make_sink(spec: dict) -> FindingSink:
    kind = spec.get("type")
    if kind == "jsonl":
        return JsonlSink(spec["path"])
    if kind == "memory":
        return MemorySink()
    raise ConfigError(f"consume_plane.sinks: unsupported type {kind!r}")


async def build_manager(cfg: ConsumePlaneConfig, *, source: EventSource | None = None,
                        reader: TrajectoryReader | None = None, sinks: Sequence[FindingSink] | None = None,
                        channel: FeedbackChannel | None = None, extra_plugins: Iterable[type] = ()) -> ConsumerManager:
    registry = await load_registry(cfg, extra_plugins)
    return ConsumerManager(
        source=source or make_source(cfg),
        reader=reader or make_reader(cfg),
        registry=registry,
        ledger=Ledger(cfg.ledger_path),
        sinks=sinks if sinks is not None else [make_sink(s) for s in cfg.sinks],
        feedback=FeedbackController(cfg.feedback, channel or MemoryFeedbackChannel()),
        partitions=cfg.partitions,
        plugin_timeout_s=cfg.plugin_timeout_s,
        max_attempts=cfg.max_attempts,
        retry_backoff_s=cfg.retry_backoff_s,
        batch_size=cfg.source.batch_size,
        poll_timeout_s=cfg.source.poll_timeout_s,
    )
