"""Shared fixtures.

Unit tests run anywhere. Database tests need a disposable PostgreSQL and
read its URL from TEST_DATABASE_URL; without it they are skipped. They
wipe that database, so never point it at anything you care about.
"""

import os
from datetime import date

import psycopg
import pytest


from seed.config import load_settings
from seed.safety import is_local

FIXED_START = date(2026, 10, 14)


@pytest.fixture
def settings():
    return load_settings(mode="dev", seed=42, start_date=FIXED_START)


@pytest.fixture(scope="session")
def db_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping database tests")
    if not is_local(url):
        pytest.exit("TEST_DATABASE_URL must point to a local, disposable database", returncode=2)
    try:
        psycopg.connect(url, connect_timeout=5).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"test database not reachable: {exc}")
    return url
