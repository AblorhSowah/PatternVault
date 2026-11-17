"""``python -m app`` / ``patternvault-backend`` entry point."""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

import uvicorn

from app.config import DEFAULT_PORT, RuntimeSettings
from app.main import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patternvault-backend", description="PatternVault local backend")
    parser.add_argument("--home", type=Path, help="data directory (default: $PATTERNVAULT_HOME or ~/.patternvault)")
    parser.add_argument("--port", type=int, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    overrides: dict[str, object] = {}
    if args.home:
        overrides["home"] = args.home
    if args.port:
        overrides["port"] = args.port
    settings = RuntimeSettings(**overrides)  # type: ignore[arg-type]
    token = settings.token
    if not token:
        token = secrets.token_urlsafe(32)
        print(f"PatternVault API token (this run only): {token}", file=sys.stderr, flush=True)
    logging.basicConfig(level=settings.log_level.upper())
    app = create_app(settings, token=token)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level, access_log=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
