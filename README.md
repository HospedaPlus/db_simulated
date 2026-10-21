# HospedaPlus F3 database 

The simulated operational database of HospedaPlus' own booking engine
(source **F3** in the project statement): schema, a reproducible seed and
health checks, so every other component can build and test against the
same data.

- 28 hotels in 5 countries (Colombia, Mexico, Brazil, Portugal, Spain) and exactly
  4,000 rooms, read from the **shared catalog** every source uses
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
source .venv/bin/activate         # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[test]"          # or skip the three lines above: uv sync --extra test

python -m seed all                # init + seed + checks
```

The `.venv` folder is created on each machine and never committed (it's in
`.gitignore`): it holds binaries for your OS and Python version only.
If PowerShell refuses to run `Activate.ps1`, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Expected ending:

```
  PASS  inventory available = total - sold  none found
  ...
  21/21 checks passed
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
catalog/catalog.json ► shared catalog: hotels, room types, channels, seasons
seed/catalog.py ─► loads it + F3-only prices and guest names
seed/generator.py ► builds every row in memory from the seed
seed/db.py ──────► TRUNCATE + COPY in one transaction
seed/checks.py ──► health and integrity checks, fingerprints
seed/safety.py ──► blocks destructive commands on remote databases
```

**Reproducible.** Each table uses its own random stream derived from the
seed, and the tables are truncated with `RESTART IDENTITY`, so ids and
values come out the same every time. The window is
`start_date - history` to `start_date + horizon` (dev: 90 + 30 nights,
full: 400 + 180). The start date defaults to today so data stays current;
pin `SEED_START_DATE` to reproduce an old run.

**Realistic dates.** City hotels get more arrivals Sunday to Tuesday
(business trips), resorts on Friday and Saturday (getaways), and high season
brings more guests than low season. Corporate guests stay 1 to 3 nights and
book late; resort guests book further ahead. A future booking is only kept
if it would already have been made by today, so occupancy drops the further
ahead you look, as in a real booking curve. All these habits live in one
block at the top of `seed/generator.py` and are easy to tune.

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
- hotels, channels and room types identical to the shared catalog version
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
TEST_DATABASE_URL=postgresql://hospedaplus:hospedaplus@localhost:5437/hospedaplus_test pytest
# PowerShell:
# $env:TEST_DATABASE_URL="postgresql://hospedaplus:hospedaplus@localhost:5437/hospedaplus_test"; pytest
```

Create the test database once with
`docker exec -it hospedaplus-f3-db createdb -U hospedaplus hospedaplus_test`.
The database tests also prove that 50 concurrent buyers of the last room
produce exactly one sale. CI (`.github/workflows/ci.yml`) runs everything
against a PostgreSQL service on every push.

## Shared database (Neon)

The team's shared F3 database runs on Neon (managed PostgreSQL 16). It was
loaded in `full` mode and verified with `python -m seed check`.

| | |
|---|---|
| Provider | Neon, project `hospedaplus_database`, branch `production` |
| Region | AWS US East (Ohio), `aws-us-east-2` |
| Host | `ep-divine-cake-b48dsbxg-pooler.c-6.us-east-2.aws.neon.tech` (pooled) · `ep-divine-cake-b48dsbxg.c-6.us-east-2.aws.neon.tech` (direct) |
| Database | `hospedaplus` |
| Connection | port 5432, SSL required; direct host (no `-pooler`) for the seed, `-pooler` host for the API, consumers, Airflow and the team |
| Credentials | Neon console → **Connect**, role `hospedaplus_reader` (read-only); never commit them |
| Last full load | 2026-10-15 |

Point a single terminal at it (PowerShell shown), then go back to local:

```powershell
$env:DATABASE_URL = "postgresql://hospedaplus_reader:<password>@ep-divine-cake-b48dsbxg-pooler.c-6.us-east-2.aws.neon.tech/hospedaplus?sslmode=require"
python -m seed check             # read-only, safe to run any time
Remove-Item Env:DATABASE_URL     # back to your local database
```

- Re-seeding replaces everyone's data: agree with the team first, then
  `python -m seed seed --mode full --allow-remote`.
- Never run `pytest` against it: the database tests wipe their database
  (and `test/conftest.py` refuses non-local URLs anyway).
- Roles, pooled vs direct host, DBeaver/psql and setup: see
  [`DEPLOYEMENT.md`](DEPLOYEMENT.md).

## How it fits the project

```
F3 (this repo, PostgreSQL) ─┐
F2 channel-manager events ──┤
F4 agency files ────────────┼─► Airflow ─► S3 Bronze ─► Silver ─► Gold (dim_/fact_) ─► Power BI
F5 reviews API ─────────────┤
F1 Kaggle, F6, F7 APIs ─────┘
```

- **Shared catalog.** `catalog/catalog.json` is a pinned copy of the
  HospedaPlus shared catalog (version in the file): hotel codes
  `HP-CO-001`…, room types `SINGLE/DOUBLE/TWIN/SUITE`, channels with their
  RN-02 commission, currency per country and RN-05 seasons. F2, F4 and F5
  read the same file, so the pipeline can join every source. Never edit the
  copy: change the shared catalog, publish a new version and copy it here
  (`seed check` fails if Neon and the file disagree).
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
catalog/        catalog.json (pinned copy of the shared catalog)
sql/            schema.sql, indexes.sql, reset.sql
seed/           Python package (python -m seed)
test/           unit + database tests
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
- **`Permission denied` on `sql\schema.sql`, or "is a folder, not a file"**:
  Docker created empty folders because the SQL files weren't there when the
  container first started. Run `docker compose down -v`, delete the folders,
  copy the real files back and start again.
- **`Refusing to ... on remote host`**: working as intended; add
  `--allow-remote` only if you mean it.