"""Serve persisted evidence, or explicit examples with --example-mode."""
import argparse
import ipaddress
import sys
from pathlib import Path

import uvicorn

from persistence.http_api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only evidence API and configuration management")
    parser.add_argument("--evidence-dir", type=Path,
                        help="directory of per-session evidence stores (<session_id>.evidence.db); the live "
                              "pipeline passes its bank-runs directory.")
    parser.add_argument("--example-mode", action="store_true", help="explicit frontend example data, not persisted evidence")
    parser.add_argument("--config-dir", type=Path, help="shared backend configuration directory (default: CONFIG_DIR)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--cors-origin", action="append", default=[], help="allowed dashboard origin; repeatable")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit binding a non-loopback address (the API has no authentication)")
    parser.add_argument("--no-docs", action="store_true", help="disable /api/v1/docs and /api/v1/openapi.json")
    args = parser.parse_args()

    loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    if not loopback and not args.allow_remote:
        sys.exit("refusing to bind a non-loopback address without --allow-remote")
    if args.evidence_dir is not None and not args.evidence_dir.is_dir():
        sys.exit(f"--evidence-dir {args.evidence_dir} is not a directory")
    evidence_dir = args.evidence_dir.resolve() if args.evidence_dir else None
    from configuration.service import ConfigService
    app = create_app(frozenset(args.cors_origin), docs=not args.no_docs, evidence_dir=evidence_dir,
                     config_service=ConfigService(args.config_dir), example_mode=args.example_mode, config_writes=False)
    # access_log off: query strings may carry IDs and must not end up in logs.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
