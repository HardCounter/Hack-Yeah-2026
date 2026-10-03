"""python -m consume_plane --config consume_plane.yaml --replay runs/<run>/events.jsonl"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .adapters.jsonl import JsonlReplaySource
from .runtime.bootstrap import build_manager
from .runtime.config import ConfigError, load_config
from .runtime.loader import PluginLoadError


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.ledger:
        cfg.ledger_path = args.ledger if args.ledger == ":memory:" else str(Path(args.ledger).resolve())
    if args.replay:
        cfg.source.type, cfg.source.path = "jsonl_replay", str(Path(args.replay).resolve())
        cfg.trajectory.type, cfg.trajectory.path = "jsonl", cfg.source.path
        cfg.trajectory.contracts_path = str(Path(args.contracts).resolve()) if args.contracts else None
    if cfg.source.type != "jsonl_replay":
        raise ConfigError("standalone runs need a finite source: pass --replay or set source.type=jsonl_replay;"
                          " the memory source is for embedding in the all-in-one daemon")

    manager = await build_manager(cfg)
    await manager.run(stop_when_idle=True)

    source = manager.source
    summary = {
        "plugins": [f"{p.name}@{p.version}" for p in manager.registry.plugins],
        "events_acked": len(source.acked),
        "decode_errors": getattr(source, "decode_errors", []) if isinstance(source, JsonlReplaySource) else [],
        "plugin_dead_letters": manager.ledger.dead_letters(),
        "feedback": [{"plugin": d.plugin, "action": d.proposal.action, "outcome": d.reason,
                      "trigger_event_id": d.trigger_event_id} for d in manager.feedback.log],
        "metrics": manager.metrics.snapshot(),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 1 if summary["decode_errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m consume_plane", description=__doc__)
    parser.add_argument("--config", default="consume_plane.yaml")
    parser.add_argument("--replay", help="events.jsonl in wire envelope v2.1")
    parser.add_argument("--contracts", help="contracts.jsonl (one TaskContract per line)")
    parser.add_argument("--ledger", help="override ledger_path; ':memory:' reprocesses every event on each run")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return asyncio.run(_main(args))
    except (ConfigError, PluginLoadError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
