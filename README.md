# HospedaPlus F3 database 

The simulated operational database of HospedaPlus' own booking engine
(source **F3** in the project statement): schema, a reproducible seed and
health checks, so every other component can build and test against the
same data.

- 28 hotels in 5 countries (Colombia, Mexico, Brazil, Portugal, Spain) and exactly
  4,000 rooms, read from the **shared catalog** every source uses
- room types, channels with RN-02 commissions, seasonal rates (RN-05)
- daily inventory per hotel and room type, 365 nights ahead, with a
  controlled **overbooking limit** (RN-03, 0 by default)
- one booking function, `operation.reservar()`, that every channel uses
  (F3 direct, F2 OTAs, F4 agencies), with rejections stored (RN-04)
- cancellation policies per channel and penalties in the hotel's time zone (RN-07)
- 60,000+ guests with the same `guest_ref` and loyalty tier as F2's events
- direct and corporate reservations, with cancellations and no-shows
- a **simulator** that keeps direct bookings arriving (F3 is a continuous source)
- configurable bad rows for the data-quality layer (statement 5.3)

Everything lives in the **`operation`** schema. F2's event ledger sits next
to it in the `streaming` schema of the same database.

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
  PASS  inventory sold = confirmed bookings  none found
  ...
  24/24 checks passed
```

No Python on your machine? Run the seed in a container instead:
`docker compose run --rm seed all`.

**Neon with Docker.** Put `NEON_OWNER_URL` (role `hospedaplus_owner`, host
without `-pooler`) and `NEON_APP_URL` (role `app_writer`, `-pooler` host) in
`.env`, then:

```bash
docker compose run --rm --build neon-seed   # schema operation + full data + checks on Neon
docker compose up --build neon-simulator    # continuous direct bookings into Neon (Ctrl+C)
```

## Commands

| Command | What it does | Destructive |
|---|---|---|
| `python -m seed init` | Creates tables and indexes (`IF NOT EXISTS`) | No |
| `python -m seed seed` | Empties the tables and loads a fresh dataset in one transaction | Yes |
| `python -m seed check` | Connectivity, tables, counts and relationship checks | No |
| `python -m seed all` | `init` + `seed` + `check` | Yes |
| `python -m seed reset` | Drops every table (use after changing `schema.sql`) | Yes |
| `python -m seed fingerprint` | md5 per table, to prove two runs are identical | No |
| `python -m seed simulate` | Keeps direct/corporate bookings arriving through `reservar()` | No (adds rows) |
| `python -m seed events` | Loads `data/local_events.csv` (F6b) into `operation.local_events` and refreshes overbooking limits (0 on "high" events) | Replaces only the events table |

Options: `--mode dev|full`, `--seed 42`, `--start-date 2026-10-14`,
`--allow-remote`, `--json` (for `check` and `fingerprint`),
`--per-minute 6` and `--duration 0` (for `simulate`).

Everything can also come from `.env` (see `.env.example`):
`DATABASE_URL`, `SEED_MODE`, `SEED_RANDOM_SEED`, `SEED_START_DATE`,
`SEED_ZERO_GUESTS_RATE`, `SEED_INVALID_DATES_RATE`, `SIM_BOOKINGS_PER_MIN`.

## How it works

```
sql/schema.sql ──► schema operation, tables, keys ┐
sql/indexes.sql ─► secondary indexes              ├─ python -m seed init
sql/functions.sql► reservar(), liberar(), ...     ┘
catalog/catalog.json ► shared catalog: hotels, room types, channels, seasons
seed/catalog.py ─► loads it + F3-only prices and guest names
seed/generator.py ► builds every row in memory from the seed
seed/db.py ──────► TRUNCATE + COPY in one transaction
seed/checks.py ──► health and integrity checks, fingerprints
seed/safety.py ──► blocks destructive commands on remote databases
seed/simulate.py ► continuous bookings + a new night on sale every day
```

**Reproducible.** Each table uses its own random stream derived from the
seed, and the tables are truncated with `RESTART IDENTITY`, so ids and
values come out the same every time. The window is
`start_date - history` to `start_date + horizon` (dev: 90 + 365 nights,
full: 400 + 365; inventory and rates always cover a year ahead). The start date defaults to today so data stays current;
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

**One way to book: `operation.reservar()`.** Every writer (FastAPI, the
F2 Kafka consumer, the F4 agencies DAG, the simulator) calls the same
database function with catalog codes:

```sql
SELECT * FROM operation.reservar('direct_web', 'WEB-123', 'HP-CO-001', 'DOUBLE', 'G-0000001',
                                 DATE '2026-12-01', DATE '2026-12-03', 1, 2, true);
--  result    | reservation_code | reservation_id | nights_without_room
--  confirmed | WEB-123          |          52793 | {}
```

It locks the nights with `SELECT ... FOR UPDATE` (always in date order, so
no deadlocks) and sells only if `sold_rooms < total_rooms + overbooking_limit`
every night. Otherwise it returns `rejected`, with the full nights, and keeps
a row in `reservation_rejections` (RN-04). The same `(channel, external
code)` twice returns `already_exists`: Kafka redeliveries and re-run DAGs
don't book twice. Its companions:

| Function | What it does |
|---|---|
| `modificar(channel, external_code, room_type, check_in, check_out, ...)` | a channel changes a booking: old nights freed and new ones taken in one step; `rejected` (booking unchanged) if the new stay has no room; `not_found`, `stale` or `not_modifiable` otherwise. NULL keeps a value |
| `liberar(code, status)` | `cancelled`, `no_show`, `relocated` or `duplicate`: gives the nights back (only once) |
| `penalidad(code, at)` | what cancelling at `at` would cost (RN-07, check-in 15:00 hotel time); changes nothing |
| `cancelar(code, at)` | cancels, stores `penalty_amount` and frees the nights |
| `refresh_overbooking_limits(today)` | daily RN-03 limits from `cancellation_rate_by_lead`; 0 on nights with a `high` event in `local_events` |

**Overbooking under control.** `inventory.overbooking_limit` starts at 0,
so nothing is oversold until the daily DAG computes a limit. The database
rejects `sold_rooms > total_rooms + overbooking_limit` anyway, and
`available_rooms` is computed by the database. The seed derives `sold_rooms`
from its confirmed reservations, so the two always agree.

**Guests shared with F2.** `guests.guest_ref` is `G-0000001`… and
`loyalty_tier` follows F2's rule (`seed/catalog.py`, copied from
`buenavista-streaming/src/events.py`), so a guest an OTA event names is the
same person, with the same tier, here. Gold guests are never relocated (RN-09).

**Safe by default.** `seed`, `all` and `reset` stop if `DATABASE_URL`
points anywhere other than localhost (or the compose service `postgres`).
Add `--allow-remote` when you really mean to load Neon.

**Bad data on purpose.** Rows with zero guests or check-out before check-in
would break the constraints, so they go to `reservation_anomalies`
(2% + 2% by default, set the rates to `0` to turn them off).

## Checks (acceptance criterion 4)

`python -m seed check` exits with code 0 only if all of these pass:

- connectivity; all 14 tables and the 6 booking functions in `operation`
- counts: 28 hotels, 4,000 rooms, 4 room types, 21 channels, 60,000+ guests,
  16 cancellation-policy rows, data in the rest
- hotels, channels (with their source) and room types identical to the shared catalog
- guest tiers follow F2's rule
- no orphan rooms or reservations
- inventory total = physical rooms; sold = room-nights of confirmed bookings
- every night has exactly one rate; rates and bookings use the hotel's currency
- guests fit the room capacity; each booking's source matches its channel
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
The database tests (`test/test_booking.py`) prove the solution's acceptance
criteria against a real PostgreSQL:

| Test | Criterion |
|---|---|
| 50 concurrent `reservar()` for the last room, limit 0 → 1 confirmed, 49 rejections stored | CA-08, E-15, RN-04 |
| Same with limit 2 → exactly 3 confirmed | CA-08 |
| Night with a `high` local event → limit 0 after `refresh_overbooking_limits()` | CA-02 |
| Cancel 48 h before a refundable stay → 50% of the first night; 100 h → 0; no-show → 100%; non-refundable → 100% of the total | CA-05, RN-07 |
| Same booking sent twice → stored once | RNF-05 |
| Cancelling frees the nights, and only once | |
| The simulator books and keeps a year on sale | |

CI (`.github/workflows/ci.yml`) runs everything against a PostgreSQL 16
service on every push.

## Continuous source: the simulator

```bash
python -m seed simulate                 # 6 bookings a minute until Ctrl+C
docker compose up simulator             # same thing in a container
```

Every few seconds it books a direct or corporate stay through
`operation.reservar()` (some get `rejected` when the hotel is full), and now
and then a guest cancels through `operation.cancelar()`. Once a day it puts a
new night on sale 365 days ahead. Watch it work:

```bash
docker exec -it hospedaplus-f3-db psql -U hospedaplus -c "SELECT source, status, count(*) FROM operation.reservations WHERE created_at > now() - interval '5 minutes' GROUP BY 1, 2;"
```

## Shared database (Neon)

The team's shared F3 database runs on Neon (managed PostgreSQL 16), database
`hospedaplus`, schema `operation`, branches `development` and `production`.
Load `development` first. Roles: `hospedaplus_owner` (seed), `app_writer`
(API, consumers, simulator), `etl_reader` (read-only).

| | |
|---|---|
| Provider | Neon, project `hospedaplus_database`, branches `development` and `production` |
| Region | AWS US East (Ohio), `aws-us-east-2` |
| Host | `ep-divine-cake-b48dsbxg-pooler.c-6.us-east-2.aws.neon.tech` (pooled) · `ep-divine-cake-b48dsbxg.c-6.us-east-2.aws.neon.tech` (direct) |
| Database | `hospedaplus` |
| Connection | port 5432, SSL required; direct host (no `-pooler`) for the seed, `-pooler` host for the API, consumers, Airflow and the team |
| Credentials | Neon console → **Connect**, role `etl_reader` (read-only); never commit them |

Point a single terminal at it (PowerShell shown), then go back to local:

```powershell
$env:DATABASE_URL = "postgresql://etl_reader:<password>@ep-divine-cake-b48dsbxg-pooler.c-6.us-east-2.aws.neon.tech/hospedaplus?sslmode=require"
python -m seed check             # read-only, safe to run any time
Remove-Item Env:DATABASE_URL     # back to your local database
```

- Re-seeding replaces everyone's F3 data: agree with the team first, then,
  as `hospedaplus_owner`, `python -m seed init` and
  `python -m seed seed --mode full --allow-remote`.
- Never run `pytest` against it: the database tests wipe their database
  (and `test/conftest.py` refuses non-local URLs anyway).
- Roles, pooled vs direct host, DBeaver/psql and setup: see
  [`DEPLOYMENT.md`](DEPLOYMENT.md).

## One database, two schemas (F2 + F3)

```
Neon · database hospedaplus (branches development and production)
├── schema operation   ← this repo (F3): hotels, inventory, rates, guests, reservations,
│                        reservar() / liberar() / penalidad() / cancelar()
└── schema streaming   ← buenavista-streaming (F2): reservation_event_ledger (Kafka events)
```

- **F3 → `operation`.** Loaded by the seed, then kept alive by the simulator
  (direct and corporate bookings through `reservar()`).
- **F2 → `streaming`.** The Kafka consumer stores every OTA event in the
  ledger. **Still to do in F2:** in the same transaction as the ledger
  insert, apply the event to `operation`, so OTA bookings take rooms from
  the same inventory:

  | F2 event | Call in `operation` |
  |---|---|
  | `reservation.created` | `reservar(channel_code, reservation_id, hotel_id, room_type, guest_ref, check_in, check_out, 1, guests, refundable, 0, 0, nightly_rate, occurred_at)` |
  | `reservation.updated` | `modificar(channel_code, reservation_id, room_type, check_in, check_out, 1, guests, 0, 0, nightly_rate, occurred_at)`; `not_found` = the update arrived before the creation: keep it pending in `streaming` and apply it after `reservar()` |
  | `reservation.cancelled` | `cancelar('F2-' \|\| reservation_id, occurred_at)` |
  | `reservation.no_show` | `liberar('F2-' \|\| reservation_id, 'no_show')` |

  `result = 'rejected'` means the hotel was full: the rejection is already in
  `reservation_rejections` (RN-04), so the consumer commits and moves on.
  The consumer must connect as `app_writer`, and F3 must be loaded in the
  same Neon branch first.

## Deploying F3

The database is already managed (Neon), so deploying F3 means running two
jobs with the image in this repo's `Dockerfile`:

| What | When | How | Role (URL) |
|---|---|---|---|
| **Seed** (`all --allow-remote`) | once per branch, or to reset the data | laptop: `docker compose run --rm neon-seed` · AWS: one-off **ECS Fargate task** | `hospedaplus_owner`, direct host |
| **Simulator** (`simulate`) | all the time during the demo (F3 is a continuous source) | laptop/EC2: `docker compose up -d neon-simulator` · AWS: **ECS Fargate service**, 1 task | `app_writer`, `-pooler` host |

On AWS:

1. Build and push the image to ECR (`docker build -t hospedaplus-f3 .`,
   then tag and push to `<account>.dkr.ecr.us-east-2.amazonaws.com/hospedaplus-f3`).
2. Store the two URLs in **Secrets Manager** (`hospedaplus/neon/owner`,
   `hospedaplus/neon/app_writer`); each task definition maps its secret to
   `DATABASE_URL`. Never put a URL in the image or the repo.
3. Seed task: command `["all", "--allow-remote"]`, `SEED_MODE=full`, run it
   once and check the logs end with `24/24 checks passed`.
4. Simulator service: command `["simulate"]`, `SIM_BOOKINGS_PER_MIN=6`,
   desired count 1 (two copies would just double the rate). Logs go to
   CloudWatch, one line per booking.
5. Airflow's bronze worker reads `operation` with `etl_reader`, incrementally
   by `created_at`.

The same steps work against `development` first and `production` after.
The simulator keeps Neon's compute awake, so stop it (desired count 0) when
you are not demoing.

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
- **Booking API (FastAPI), F2 consumer, F4 DAG.** Read availability and
  rates from `inventory` and `rates`; book with `operation.reservar()`,
  cancel with `operation.cancelar()`, show penalties with
  `operation.penalidad()`. The F2 consumer calls them in the same
  transaction as its ledger insert.
- **Airflow.** `limites_sobreventa_diario`: Spark writes
  `cancellation_rate_by_lead`, F6 loads `local_events`, then
  `refresh_overbooking_limits()`. `reubicacion_diaria`: writes
  `relocations` and calls `liberar(code, 'relocated')`.
- **Pipeline.** Extracts `reservations`, `reservation_anomalies`,
  `reservation_rejections` and `relocations` into Bronze (incrementally, by
  `created_at` / `rejected_at`); Silver must catch the anomalies; personal
  data in `guests` must be pseudonymized before Gold (RN-11).
- **Not here.** Dynamic pricing (pickup and event multipliers, RN-05/06)
  and duplicate detection (RN-08: `status = 'duplicate'` and `duplicate_of`
  are ready for it) belong to the services that consume this database.

## Files

```
catalog/        catalog.json (pinned copy of the shared catalog)
sql/            schema.sql, indexes.sql, functions.sql, reset.sql
seed/           Python package (python -m seed)
test/           unit + database tests (test_booking.py: reservar() and friends)
DEPLOYMENT.md   Neon: load, roles, connections, simulator
Readmeexe.md    quick guide in Spanish: run everything on Neon with Docker
Dockerfile      seed job image (local and ECS Fargate)
docker-compose.yml, .env.example, .github/workflows/ci.yml
```

## Troubleshooting

- **`Can't reach the database`**: is the container up (`docker ps`)? Does
  the port in `DATABASE_URL` match `POSTGRES_PORT`?
- **`Tables are missing`**: run `python -m seed init`.
- **Changed `schema.sql` but nothing changed**: `IF NOT EXISTS` skips
  existing tables. Run `python -m seed reset` and then `python -m seed all`.
  With Docker: `docker compose down -v` and start again.
- **`column "sold_rooms" does not exist`** or tables still in `public`:
  your database has the old schema. Same fix as above.
- **`Permission denied` on `sql\schema.sql`, or "is a folder, not a file"**:
  Docker created empty folders because the SQL files weren't there when the
  container first started. Run `docker compose down -v`, delete the folders,
  copy the real files back and start again.
- **`Refusing to ... on remote host`**: working as intended; add
  `--allow-remote` only if you mean it.