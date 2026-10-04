"""Command line: database migrations and a queue drain for debugging.

    uv run python -m app.cli migrate        apply pending migrations
    uv run python -m app.cli status         list applied / pending migrations
    uv run python -m app.cli run-pending    execute queued test runs in this process, then exit
"""
from __future__ import annotations

import argparse
import sys

from .config import Settings
from .db import Database, applied_migrations, migrate, pending_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["migrate", "status", "run-pending"])
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    if args.command == "migrate":
        applied = migrate(db)
        print(f"Database: {settings.db_path}")
        print("Applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    elif args.command == "status":
        print(f"Database: {settings.db_path}")
        print("Applied: " + (", ".join(applied_migrations(db)) or "none"))
        print("Pending: " + (", ".join(pending_migrations(db)) or "none"))
    else:
        from .execution.worker import run_pending

        if pending_migrations(db):
            print("Pending migrations; run 'migrate' first.", file=sys.stderr)
            return 1
        from .planning.llm import build_llm

        print(f"Executed {run_pending(db, settings, llm=build_llm(settings))} test run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
