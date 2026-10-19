# Seed job image: runs `python -m seed <command>` against DATABASE_URL.
# Same image locally (docker compose) and on AWS (ECS Fargate task).
FROM python:3.12-slim

# No .pyc files, logs straight to stdout (CloudWatch picks them up).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so this layer is cached between code changes.
RUN pip install "psycopg[binary]>=3.2,<4" tzdata

COPY seed/ seed/
COPY sql/ sql/
COPY catalog/ catalog/

# Don't run as root.
RUN useradd --create-home --uid 10001 seeder
USER seeder

ENTRYPOINT ["python", "-m", "seed"]
CMD ["all"]
