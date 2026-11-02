"""Command line entry point: `python -m seed <command>`.

    init         create tables and indexes (safe, never drops)
    seed         replace all data with a fresh, reproducible dataset
    check        health and integrity checks
    all          init + seed + check, the usual way to start
    reset        drop every table (destructive)
    fingerprint  print a hash per table to compare two runs
    simulate     keep direct/corporate bookings arriving (continuous source)
    events       load the F6 local events CSV and refresh the overbooking limits
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

import psycopg

from . import checks, db, events, simulate
from .config import MODES, load_settings
from .generator import generate
from .safety import RemoteDatabaseError, ensure_safe, redact


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m seed", description="HospedaPlus F3 database seed")
    p.add_argument("command", choices=["init", "seed", "check", "all", "reset", "fingerprint", "simulate", "events"])
    p.add_argument("--mode", choices=sorted(MODES), help="dev (small) or full (default: SEED_MODE or dev)")
    p.add_argument("--seed", type=int, help="random seed (default: SEED_RANDOM_SEED or 42)")
    p.add_argument("--start-date", type=date.fromisoformat,
                   help="simulation 'today', YYYY-MM-DD (default: SEED_START_DATE or today)")
    p.add_argument("--allow-remote", action="store_true", default=None,
                   help="allow reset/seed against a non-local database")
    p.add_argument("--json", action="store_true", help="machine-readable output for check/fingerprint")
    p.add_argument("--per-minute", type=float, default=float(os.getenv("SIM_BOOKINGS_PER_MIN", "6")),
                   help="simulate: bookings per minute (default: SIM_BOOKINGS_PER_MIN or 6)")
    p.add_argument("--duration", type=float, default=float(os.getenv("SIM_DURATION_SECONDS", "0")),
                   help="simulate: stop after N seconds (default 0 = run until Ctrl+C)")
    return p


def _seed(settings) -> None:
    t0 = time.perf_counter()
    ds = generate(settings)
    counts = db.load(settings.database_url, ds)
    print(f"Seeded {redact(settings.database_url)}")
    print(f"  mode={settings.mode.name} seed={settings.seed} start_date={settings.start_date} "
          f"window={ds.window_start}..{ds.window_end} (end exclusive)")
    for table, n in counts.items():
        print(f"  {table:<21} {n:>9,}")
    print(f"  bookings refused because the room type was full: {ds.rejected_full:,}")
    print(f"  done in {time.perf_counter() - t0:.1f}s")


def _check(settings, as_json: bool) -> int:
    results = checks.run_checks(settings.database_url)
    if as_json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(f"Checks on {redact(settings.database_url)}")
        checks.print_results(results)
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(mode=args.mode, seed=args.seed, start_date=args.start_date,
                             allow_remote=args.allow_remote)
    url = settings.database_url
    if not url.strip():
        # e.g. `docker compose run neon-seed` with NEON_OWNER_URL missing from .env
        print("DATABASE_URL is empty. For the neon-* Docker services, set NEON_OWNER_URL "
              "(and NEON_APP_URL for the simulator) in .env.", file=sys.stderr)
        return 2
    try:
        if args.command == "init":
            db.init_schema(url)
            print("Schema `operation` ready (tables, indexes and booking functions).")
        elif args.command == "reset":
            ensure_safe(url, settings.allow_remote, "drop all tables")
            db.drop_schema(url)
            print("All F3 tables dropped. Run `python -m seed all` to rebuild.")
        elif args.command == "seed":
            ensure_safe(url, settings.allow_remote, "replace all data")
            _seed(settings)
        elif args.command == "check":
            return _check(settings, args.json)
        elif args.command == "all":
            ensure_safe(url, settings.allow_remote, "replace all data")
            db.init_schema(url)
            _seed(settings)
            print()
            return _check(settings, args.json)
        elif args.command == "simulate":
            # Only adds bookings (never deletes), so no --allow-remote needed.
            simulate.run(url, args.per_minute, args.duration, seed=args.seed)
        elif args.command == "events":
            # Only replaces the small events table (reference data), so no --allow-remote needed.
            loaded, changed = events.load_events(url)
            print(f"Loaded {loaded} local events into {redact(url)}; "
                  f"overbooking limits changed on {changed:,} inventory nights")
        elif args.command == "fingerprint":
            fp = checks.fingerprint(url)
            print(json.dumps(fp, indent=2) if args.json
                  else "\n".join(f"  {t:<22} {h}" for t, h in fp.items()))
    except RemoteDatabaseError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # invalid events CSV
        print(f"Invalid events file: {exc}", file=sys.stderr)
        return 1
    except psycopg.errors.UndefinedTable:
        print("Tables are missing. Run `python -m seed init` (or `all`) first.", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"Can't reach the database at {redact(url)}:\n  {exc}", file=sys.stderr)
        return 1
    return 0
