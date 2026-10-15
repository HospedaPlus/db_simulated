# Deploying the F3 database

The repo only needs a PostgreSQL 14+ database and a `DATABASE_URL`. The
same commands work on your laptop, on Supabase and on Amazon RDS; only the
connection string changes. Moving from one to another later costs nothing
but a new URL: the schema is plain SQL and the seed is deterministic, so
every database ends up with identical data.

Remote databases are protected: `seed`, `all` and `reset` refuse to run
unless you add `--allow-remote` (or set `SEED_ALLOW_REMOTE=true` in the
job's environment). `init` and `check` never need it.

---

## Choosing an option

Prices checked on 2026-10-15, in USD. They change: confirm in the
[AWS Pricing Calculator](https://calculator.aws/) and on
[supabase.com/pricing](https://supabase.com/pricing) before committing.

| | Local Docker | Supabase Free | Supabase Pro | RDS `db.t4g.micro` (course) | RDS `db.t4g.medium` Multi-AZ (team estimate) |
|---|---|---|---|---|---|
| Monthly cost | $0 | $0 | from $25 | ~$12 compute + ~$2–3 for 20 GB storage; $0 while the AWS Free plan or credits last | $117.17 (team AWS estimate, us-east-2) |
| Setup time | 5 min | 10 min | 10 min | 30–60 min | 1–2 h with the Fargate job |
| Who can reach it | only you | the whole team, over the internet | the whole team | only from inside the VPC (or your IP, if you open it) | only from inside the VPC |
| Size limit | your disk | 500 MB (full mode uses ~75 MB) | 8 GB included, then $0.125/GB | what you provision | what you provision |
| Backups | none | none | daily | automatic | automatic, plus a standby copy |
| Fits the AWS architecture | no | no | no | yes | yes, it is the target |

What each choice means in practice:

**Local Docker.** Free and instant, and nothing you break affects anyone.
Each teammate has their own copy, so it is not a shared source: the API and
the pipeline can't point at it. Use it for development and tests, always.

**Supabase Free.** The fastest way to give the whole team one shared
database while AWS isn't ready. The catch: a free project is **paused after
1 week without activity** (it wakes up when you restore it from the
dashboard), there are no backups (re-seeding is the backup), and only 2
active projects per account. It also sits outside the project's AWS
architecture, so it does not count as evidence for the AWS deliverables.

**Supabase Pro.** Same as Free without the pausing, with daily backups and
8 GB of disk. Only worth paying if the team needs the shared database to be
always on for weeks and AWS is still not available.

**RDS `db.t4g.micro`.** The real target service at the smallest size: same
engine, same network rules, same secrets handling as production, so what you
learn and deploy here carries over. New AWS accounts get credits and a Free
plan that covers this class for a while; older accounts may still have the
legacy 12-month free tier. The effort is in the network: a private instance
can't be reached from a laptop without opening it to your IP (temporary) or
running the seed inside the VPC (the Fargate job below).

**RDS `db.t4g.medium` Multi-AZ.** The production size from the team's AWS
estimate: a standby copy in a second availability zone takes over if the
main one fails. It is about 10 times the cost of `db.t4g.micro` and is not
needed to develop or demo the project; switch to it only for the final
production-like deployment, if at all.

**Running the seed as an ECS Fargate job** (any RDS option) adds very little:
the team estimate puts the ad-hoc seed job at ~$1.48/month, ECR storage at
~$0.50/month and each Secrets Manager secret at $0.40/month. A Fargate task
in a private subnet needs a way out to pull its image from ECR (the NAT
gateway in the estimate, ~$35/month, or VPC endpoints); that cost is shared
with the rest of the project, not specific to this seed.

**Recommendation:** Local Docker for everyone's daily work + Supabase Free
as the shared database now + RDS `db.t4g.micro` with the Fargate job when
the team reaches the AWS milestone. Keep Multi-AZ for the final demo only if
the budget allows.

---

## Option A: Supabase (quick shared database)

1. Create a project at supabase.com and save the database password.
2. Open **Connect** and copy a connection string:
   - **Direct connection** (`db.<ref>.supabase.co:5432`) is the best fit,
     but it is IPv6-only unless the project has the IPv4 add-on.
   - On an IPv4-only network use the **Session pooler**
     (`aws-<n>-<region>.pooler.supabase.com:5432`, user `postgres.<ref>`).
   - Don't use the transaction pooler (port 6543): it is meant for
     short serverless calls, not bulk loads.
3. Build the URL with SSL and run the usual commands:

```bash
export DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-<n>-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
python -m seed init
python -m seed seed --allow-remote
python -m seed check
```

PowerShell: `$env:DATABASE_URL = "postgresql://..."`.
If the password has symbols like `@`, `#` or `/`, URL-encode them
(`@` → `%40`).

---

## Option B: Amazon RDS for PostgreSQL (target)

### 1. Create the database

- RDS → Create database → PostgreSQL 16, region **us-east-2 (Ohio)** to
  match the cost estimate.
- Course/dev: `db.t4g.micro`, Single-AZ, 20 GB gp3. Production estimate:
  `db.t4g.medium`, Multi-AZ.
- **Public access: No.** Put it in private subnets of the project VPC.
- Security group: inbound 5432 only from the seed task's and the API's
  security groups (temporarily from your IP if you must load it from a laptop).
- Initial database name: `hospedaplus`.
- Store the URL in **AWS Secrets Manager** (e.g. secret
  `hospedaplus/f3/database-url`), never in the repo:
  `postgresql://<user>:<password>@<endpoint>:5432/hospedaplus?sslmode=require`

### 2a. Load it from a laptop (simplest)

Only if the instance is reachable from your IP:

```bash
export DATABASE_URL="postgresql://<user>:<password>@<endpoint>.us-east-2.rds.amazonaws.com:5432/hospedaplus?sslmode=require"
python -m seed init
python -m seed seed --mode full --allow-remote
python -m seed check
```

Close the security-group rule for your IP when you're done.

### 2b. Load it with an ECS Fargate job (as in the architecture)

The `Dockerfile` builds the seed job image; it runs `python -m seed all`
by default and logs to stdout, which ends up in CloudWatch.

```bash
AWS_REGION=us-east-2
ACCOUNT=<aws-account-id>
REPO=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/hospedaplus-f3-seed

aws ecr create-repository --repository-name hospedaplus-f3-seed --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker build -t $REPO:1.0.0 .
docker push $REPO:1.0.0
```

Task definition (Fargate, 0.5 vCPU / 1 GB is plenty):

- image `$REPO:1.0.0`, command `["all"]`
- secret `DATABASE_URL` ← Secrets Manager `hospedaplus/f3/database-url`
- environment `SEED_MODE=full`, `SEED_ALLOW_REMOTE=true`, optionally `SEED_START_DATE`
- execution role allowed to read that secret; log driver `awslogs`

Run it on demand in the same VPC and private subnets as RDS:

```bash
aws ecs run-task --cluster hospedaplus --launch-type FARGATE \
  --task-definition hospedaplus-f3-seed \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet>],securityGroups=[<seed-sg>],assignPublicIp=DISABLED}"
```

The task exits with code 0 when all checks pass and 1 otherwise, so a
failed seed is visible in ECS and in CloudWatch logs.

---

## After deploying

- Share the read-only connection details with the API and pipeline owners;
  create a separate read-only role for Airflow instead of reusing the admin user.
- Re-seeding wipes the F3 tables (and anything that references them through
  foreign keys, because of `TRUNCATE ... CASCADE`). Agree with the team
  before doing it on a shared database.
- Compare two databases (say Supabase and RDS) with
  `python -m seed fingerprint` run against each, with the same
  `SEED_START_DATE`: identical hashes mean identical data.
- Stop or delete RDS instances you're not using: they bill every hour they
  run, used or not.