"""Settings, read from environment variables (or a local .env file).

Same seed + same start date + same mode = exactly the same data.
The start date defaults to today so the data never looks stale; pin
SEED_START_DATE when you need to reproduce an older run byte for byte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    # Tiny .env reader so we don't need an extra dependency.
    # Real environment variables always win over the file.
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].split("\t#", 1)[0]  # drop inline comments
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# Where schema.sql, indexes.sql and reset.sql live (overridable for containers).
SQL_DIR = Path(os.getenv("SEED_SQL_DIR", ROOT / "sql"))

DEFAULT_DATABASE_URL = "postgresql://hospedaplus:hospedaplus@localhost:5432/hospedaplus"


@dataclass(frozen=True)
class Mode:
    """How much data to generate. The catalog (28 hotels, ~4,000 rooms) is
    always complete because other sources join on it; only the date window
    and the number of guests change."""

    name: str
    history_days: int   # nights before the start date (past stays, no-shows)
    horizon_days: int   # nights from the start date onwards (future bookings)
    guests: int


MODES = {
    "dev": Mode("dev", history_days=90, horizon_days=30, guests=40_000),
    "full": Mode("full", history_days=400, horizon_days=180, guests=100_000),
}


@dataclass(frozen=True)
class Settings:
    database_url: str
    seed: int
    mode: Mode
    start_date: date
    zero_guests_rate: float
    invalid_dates_rate: float
    allow_remote: bool


def _rate(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")
    return value


def load_settings(**overrides) -> Settings:
    """Build settings from env vars; CLI flags come in as overrides."""
    start = os.getenv("SEED_START_DATE")
    mode_name = os.getenv("SEED_MODE", "dev")
    values = dict(
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        seed=int(os.getenv("SEED_RANDOM_SEED", "42")),
        mode=MODES[mode_name],
        start_date=date.fromisoformat(start) if start else date.today(),
        # Defaults match the US-26 defect catalog (2% each).
        zero_guests_rate=_rate("SEED_ZERO_GUESTS_RATE", "0.02"),
        invalid_dates_rate=_rate("SEED_INVALID_DATES_RATE", "0.02"),
        allow_remote=os.getenv("SEED_ALLOW_REMOTE", "false").lower() in ("1", "true", "yes"),
    )
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "mode":
            value = MODES[value]
        values[key] = value
    return Settings(**values)
