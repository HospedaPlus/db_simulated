"""Guard rail for destructive commands.

`reset` drops tables and `seed` empties them before loading. That's
fine on your laptop and a disaster on the shared AWS database, so both
refuse to run against anything that isn't local unless you say so
explicitly (--allow-remote or SEED_ALLOW_REMOTE=true).
"""

from __future__ import annotations

from urllib.parse import urlparse

# "postgres" is the service name inside docker-compose, also local.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "host.docker.internal"}


class RemoteDatabaseError(RuntimeError):
    pass


def database_host(url: str) -> str:
    host = urlparse(url).hostname
    return host or "localhost"  # no host = unix socket on this machine


def is_local(url: str) -> bool:
    return database_host(url) in LOCAL_HOSTS


def ensure_safe(url: str, allow_remote: bool, action: str) -> None:
    if is_local(url) or allow_remote:
        return
    raise RemoteDatabaseError(
        f"Refusing to {action} on remote host '{database_host(url)}'. "
        "This would wipe shared data. If you really mean it, "
        "re-run with --allow-remote (or SEED_ALLOW_REMOTE=true)."
    )


def redact(url: str) -> str:
    """Hide the password before printing a connection string."""
    parsed = urlparse(url)
    if parsed.password:
        return url.replace(f":{parsed.password}@", ":***@")
    return url

