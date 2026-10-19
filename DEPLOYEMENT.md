# Deployment: F3 database on Neon

The shared F3 database runs on [Neon](https://neon.com) (managed PostgreSQL),
in AWS US East (Ohio). The repo only needs a `DATABASE_URL`, so nothing in
the code is Neon-specific.

| | |
|---|---|
| Neon project | `hospedaplus_database`, branch `production`, Free plan |
| Region | AWS US East (Ohio), `aws-us-east-2` |
| Database | `hospedaplus`, PostgreSQL 16, owner role `hospedaplus_owner` |
| Data | `full` mode, loaded 2026-10-15 from a laptop, **old catalog**: reload to move to shared catalog 1.0.0 |
| Health | `python -m seed check`: 20/20 passed (21 checks after the reload) |

![Health evidence](image.png)

## How it was set up

1. **Project.** In [console.neon.tech](https://console.neon.tech): new project
   `hospedaplus_database`, Postgres database `hospedaplus`, version 16,
   region AWS US East (Ohio). All other services (object storage, functions,
   AI gateway, realtime, auth) left off.
2. **Connection string.** **Connect** → branch `production`, database
   `hospedaplus`, **connection pooling off**:

   ```
   postgresql://hospedaplus_owner:<password>@<endpoint>.c-6.us-east-2.aws.neon.tech/hospedaplus?sslmode=require&channel_binding=require
   ```

   The direct host (no `-pooler`) is for the seed, which creates tables and
   bulk-loads with `COPY`. The `-pooler` host suits clients with many short
   connections, such as the API.
3. **Load and check**, from a terminal with the URL set only for that session:

   ```powershell
   $env:DATABASE_URL = "<connection string>"
   python -m seed init
   python -m seed seed --mode full --allow-remote
   python -m seed check
   Remove-Item Env:DATABASE_URL
   ```

   `--allow-remote` is required for any command that replaces data on a
   non-local database. The load runs in one transaction: if it fails half
   way, Neon keeps its previous data.

## Good to know

- **Free plan limits:** 1 GB storage (full mode uses ~75 MB) and 100
  CU-hours of compute per month. The database sleeps after 5 minutes idle and
  wakes on the next query, so batch jobs stay free; something querying it
  around the clock would not.
- **Credentials** are shared privately, never committed. If they leak, reset
  them in Neon (**Postgres database → Roles → Reset password**, or
  `ALTER ROLE hospedaplus_owner WITH PASSWORD '...'` in the SQL Editor) and
  update everyone's URL.
- **Never run `pytest` against Neon**: the database tests wipe their
  database. `python -m seed check` is read-only and safe at any time.

## Next steps

- [ ] **EC2 in us-east-2 for repeatable loads**, so anyone on the team can
      reload Neon without a laptop (repo + `.venv` + a git-ignored `.env`
      with the Neon URL).
- [ ] **Scheduled reload** (cron on the EC2 or an Airflow task), if the team
      wants the date window to move forward automatically.
- [ ] **Read-only role for Airflow**, instead of sharing the owner role.
- [ ] **ADR** recording the choice of Neon over RDS and Postgres on EC2,
      with costs and trade-offs.