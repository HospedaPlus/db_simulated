import pytest

from seed.safety import RemoteDatabaseError, ensure_safe, is_local, redact


@pytest.mark.parametrize("url", [
    "postgresql://u:p@localhost:5432/db",
    "postgresql://u:p@127.0.0.1/db",
    "postgresql://u:p@postgres:5432/db",   # docker-compose service name
    "postgresql:///db",                     # unix socket
])
def test_local_urls_are_allowed(url):
    assert is_local(url)
    ensure_safe(url, allow_remote=False, action="seed")


def test_remote_url_is_blocked_unless_allowed():
    url = "postgresql://u:p@hospedaplus.abc.us-east-2.rds.amazonaws.com:5432/db"
    with pytest.raises(RemoteDatabaseError):
        ensure_safe(url, allow_remote=False, action="seed")
    ensure_safe(url, allow_remote=True, action="seed")  # explicit opt-in works


def test_password_is_hidden_in_logs():
    assert redact("postgresql://u:s3cret@host/db") == "postgresql://u:***@host/db"
