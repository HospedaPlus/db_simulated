# HospedaPlus F3 database (US-23)

The simulated operational database of HospedaPlus' own booking engine
(source **F3** in the project statement): schema, a reproducible seed and
health checks, so every other component can build and test against the
same data.

- 28 hotels (26 in Colombia, 2 in Portugal) and exactly 4,000 rooms
- room types, channels with RN-02 commissions, seasonal rates (RN-05)
- daily inventory per hotel and room type, **no overbooking**
- direct and corporate reservations, with cancellations and no-shows
- configurable bad rows for the data-quality layer (statement 5.3)

Same seed + same start date = the same data, row for row.

## Requirements

| Tool | Version | Used for |
|---|---|---|
| Docker + Docker Compose | any recent | local PostgreSQL 16 |
| Python | 3.11+ | the seed and the checks |
| uv *(optional)* | any | install deps; plain `pip` works too |

## Quick start

```bash
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d postgres     # creates the tables on first start
python -m venv .venv              # an isolated environment for this project
.venv/scripts/activate         # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[test]"          # or skip the three lines above: uv sync --extra test
python -m seed all                # init + seed + checks
```

Expected ending:

```
  PASS  inventory available = total - sold  none found
  ...
  20/20 checks passed
```

No Python on your machine? Run the seed in a container instead:
`docker compose run --rm seed all`.

## Commands

| Command | What it does | Destructive |
|---|---|---|
| `python -m seed init` | Creates tables and indexes (`IF NOT EXISTS`) | No |
| `python -m seed seed` | Empties the tables and loads a fresh dataset in one transaction | Yes |
| `python -m seed check` | Connectivity, tables, counts and relationship checks | No |
| `python -m seed all` | `init` + `seed` + `check` | Yes |
| `python -m seed reset` | Drops every table (use after changing `schema.sql`) | Yes |
| `python -m seed fingerprint` | md5 per table, to prove two runs are identical | No |

Options: `--mode dev|full`, `--seed 42`, `--start-date 2026-10-14`,
`--allow-remote`, `--json` (for `check` and `fingerprint`).

Everything can also come from `.env` (see `.env.example`):
`DATABASE_URL`, `SEED_MODE`, `SEED_RANDOM_SEED`, `SEED_START_DATE`,
`SEED_ZERO_GUESTS_RATE`, `SEED_INVALID_DATES_RATE`.

## How it works

```
sql/schema.sql ──► tables, keys, constraints      ┐
sql/indexes.sql ─► secondary indexes              ├─ python -m seed init
                                                  ┘
seed/catalog.py ─► fixed hotels, room types, channels, prices
seed/generator.py ► builds every row in memory from the seed
seed/db.py ──────► TRUNCATE + COPY in one transaction
seed/checks.py ──► health and integrity checks, fingerprints
seed/safety.py ──► blocks destructive commands on remote databases
```

**Reproducible.** Each table uses its own random stream derived from the
seed, and the tables are truncated with `RESTART IDENTITY`, so ids and
values come out the same every time. The window is
`start_date - history` to `start_date + horizon` (dev: 14 + 30 nights,
full: 90 + 365). The start date defaults to today so data stays current;
pin `SEED_START_DATE` to reproduce an old run.

**Idempotent.** `seed` replaces the data instead of appending, inside one
transaction: run it ten times and you get one copy; if it fails half way,
the previous data is untouched. `init` never drops anything.

**No bookings in the past.** A trigger rejects any reservation whose
check-in is earlier than the day it was made, in the hotel's time zone.
New bookings get `created_at = now()`, so in practice: no check-in before
today. Past stays loaded by the seed are fine because they were "made"
before they started.

**No overbooking.** The generator refuses a booking when the room type is
full that night, and the database rejects `available_rooms < 0` anyway.
Inventory is derived from the reservations, so the two always agree.

**Safe by default.** `seed`, `all` and `reset` stop if `DATABASE_URL`
points anywhere other than localhost (or the compose service `postgres`).
Add `--allow-remote` when you really mean to load AWS or Supabase.

**Bad data on purpose.** Rows with zero guests or check-out before check-in
would break the constraints, so they go to `reservation_anomalies`
(2% + 2% by default, set the rates to `0` to turn them off).

## Checks (acceptance criterion 4)

`python -m seed check` exits with code 0 only if all of these pass:

- connectivity and server version; all 9 tables present
- counts: 28 hotels, 4,000 rooms, 4 room types, 21 channels, data in the rest
- no orphan rooms or reservations
- inventory total = physical rooms; available = total − room-nights sold
- every night has exactly one rate; rates and bookings use the hotel's currency
- guests fit the room capacity; F3 holds only direct/corporate bookings
- no booking starts before the day it was made (hotel local time)

## Tests

```bash
# unit tests only (no database needed)
pytest
# plus database tests: point to a throwaway database, it gets wiped
TEST_DATABASE_URL=postgresql://hospedaplus:hospedaplus@localhost:5432/hospedaplus_test pytest
# PowerShell:
# $env:TEST_DATABASE_URL="postgresql://hospedaplus:hospedaplus@localhost:5432/hospedaplus_test"; pytest
```

Create the test database once with
`docker exec -it hospedaplus-f3-db createdb -U hospedaplus hospedaplus_test`.
The database tests also prove that 50 concurrent buyers of the last room
produce exactly one sale. CI (`.github/workflows/ci.yml`) runs everything
against a PostgreSQL service on every push.

## How it fits the project

```
F3 (this repo, PostgreSQL) ─┐
F2 channel-manager events ──┤
F4 agency files ────────────┼─► Airflow ─► S3 Bronze ─► Silver ─► Gold (dim_/fact_) ─► Power BI
F5 reviews API ─────────────┤
F1 Kaggle, F6, F7 APIs ─────┘
```

- **Shared catalog.** Hotel codes `hotel-001..hotel-028`, room types
  `SINGLE/DOUBLE/TWIN/SUITE`, OTA codes and agencies `AG01..AG15` are the
  ones the F2/F4/F5 simulators already use, so the pipeline can join them.
- **Booking API (FastAPI).** Reads availability and rates from `inventory`
  and `rates`; sells with a single conditional `UPDATE` (see the
  concurrency test) to stay within the no-overbooking rule.
- **Pipeline.** Extracts `reservations` and `reservation_anomalies` into
  Bronze; Silver must catch the anomalies; personal data in `guests` must
  be pseudonymized before Gold (RN-11).
- **Not here.** Dynamic pricing (pickup and event multipliers, RN-05/06),
  duplicate resolution (RN-08) and relocation (RN-09) belong to the
  services that consume this database.

## Files

```
sql/            schema.sql, indexes.sql, reset.sql
seed/           Python package (python -m seed)
tests/          unit + database tests
docs/           data_model.md (ERD and table guide)
DEPLOYMENT.md   AWS RDS / Supabase deployment
Dockerfile      seed job image (local and ECS Fargate)
docker-compose.yml, .env.example, .github/workflows/ci.yml
```

## Troubleshooting

- **`Can't reach the database`**: is the container up (`docker ps`)? Does
  the port in `DATABASE_URL` match `POSTGRES_PORT`?
- **`Tables are missing`**: run `python -m seed init`.
- **Changed `schema.sql` but nothing changed**: `IF NOT EXISTS` skips
  existing tables. Run `python -m seed reset` and then `python -m seed all`.
- **`Refusing to ... on remote host`**: working as intended; add
  `--allow-remote` only if you mean it.
