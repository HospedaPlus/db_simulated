"""Command line entry point: `python -m seed <command>`.

    init         create tables and indexes (safe, never drops)
    seed         replace all data with a fresh, reproducible dataset
    check        health and integrity checks
    all          init + seed + check, the usual way to start
    reset        drop every table (destructive)
    fingerprint  print a hash per table to compare two runs
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

import psycopg

from . import checks, db
from .config import MODES, load_settings
from .generator import generate
from .safety import RemoteDatabaseError, ensure_safe, redact


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m seed", description="HospedaPlus F3 database seed")
    p.add_argument("command", choices=["init", "seed", "check", "all", "reset", "fingerprint"])
    p.add_argument("--mode", choices=sorted(MODES), help="dev (small) or full (default: SEED_MODE or dev)")
    p.add_argument("--seed", type=int, help="random seed (default: SEED_RANDOM_SEED or 42)")
    p.add_argument("--start-date", type=date.fromisoformat,
                   help="simulation 'today', YYYY-MM-DD (default: SEED_START_DATE or today)")
    p.add_argument("--allow-remote", action="store_true", default=None,
                   help="allow reset/seed against a non-local database")
    p.add_argument("--json", action="store_true", help="machine-readable output for check/fingerprint")
    return p


def _seed(settings) -> None:
    t0 = time.perf_counter()
    ds = generate(settings)
    counts = db.load(settings.database_url, ds)
    print(f"Seeded {redact(settings.database_url)}")
    print(f"  mode={settings.mode.name} seed={settings.seed} start_date={settings.start_date} "
          f"window={ds.window_start}..{ds.window_end} (end exclusive)")
    for table, n in counts.items():
        print(f"  {table:<13} {n:>9,}")
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
    try:
        if args.command == "init":
            db.init_schema(url)
            print("Schema ready (tables and indexes).")
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
        elif args.command == "fingerprint":
            fp = checks.fingerprint(url)
            print(json.dumps(fp, indent=2) if args.json
                  else "\n".join(f"  {t:<22} {h}" for t, h in fp.items()))
    except RemoteDatabaseError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except psycopg.errors.UndefinedTable:
        print("Tables are missing. Run `python -m seed init` (or `all`) first.", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"Can't reach the database at {redact(url)}:\n  {exc}", file=sys.stderr)
        return 1
    return 0
