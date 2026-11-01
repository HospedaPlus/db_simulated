"""Health and integrity checks (acceptance criterion 4).

Each check returns PASS/FAIL plus a short detail, so the output can be
pasted into a ticket as evidence. Run with `python -m seed check`.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .catalog import (CATALOG_VERSION, CHANNELS, F2_GUEST_POOL, HOTELS, ROOM_TYPES, TOTAL_ROOMS,
                      guest_ref, loyalty_tier)
from .db import SCHEMA, connect

# Loaded by the seed (and fingerprinted: same settings = same md5).
FINGERPRINT_TABLES = [
    "room_types", "hotels", "channels", "guests", "rooms",
    "rates", "inventory", "reservations", "reservation_anomalies", "cancellation_policies",
]
# Written by the running system (reservar(), DAGs, F6), empty after a seed.
SYSTEM_TABLES = ["reservation_rejections", "relocations", "local_events", "cancellation_rate_by_lead"]
REQUIRED_TABLES = FINGERPRINT_TABLES + SYSTEM_TABLES
REQUIRED_FUNCTIONS = ["reservar", "liberar", "penalidad", "cancelar", "refresh_overbooking_limits"]
POLICY_ROWS = 16  # RN-07: 4 rules x 4 channel types


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _one(cur, sql: str):
    cur.execute(sql)
    return cur.fetchone()[0]


def _zero(name: str, cur, sql: str, what: str) -> Result:
    """Helper for 'this query should return 0 problem rows'."""
    bad = _one(cur, sql)
    return Result(name, bad == 0, f"{bad} {what}" if bad else "none found")


def run_checks(url: str) -> list[Result]:
    results: list[Result] = []

    # 1. Connectivity
    try:
        conn = connect(url)
    except psycopg.Error as exc:
        return [Result("connectivity", False, str(exc).strip().splitlines()[0])]

    with conn, conn.cursor() as cur:
        version = _one(cur, "SHOW server_version")
        results.append(Result("connectivity", True, f"PostgreSQL {version}"))

        # 2. Required tables
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (SCHEMA,))
        present = {r[0] for r in cur.fetchall()}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        results.append(Result(f"required tables in {SCHEMA}", not missing,
                              f"missing: {', '.join(missing)}" if missing
                              else f"all {len(REQUIRED_TABLES)} present"))
        cur.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = %s", (SCHEMA,))
        functions = {r[0] for r in cur.fetchall()}
        missing_fn = [f for f in REQUIRED_FUNCTIONS if f not in functions]
        results.append(Result("booking functions", not missing_fn,
                              f"missing: {', '.join(missing_fn)}" if missing_fn
                              else ", ".join(f"{f}()" for f in REQUIRED_FUNCTIONS)))
        if missing:
            return results  # nothing else makes sense without the tables

        # 3. Record counts
        counts = {t: _one(cur, f"SELECT count(*) FROM {t}") for t in REQUIRED_TABLES}
        results.append(Result("hotels count", counts["hotels"] == len(HOTELS),
                              f"{counts['hotels']} (expected {len(HOTELS)})"))
        results.append(Result("rooms count", counts["rooms"] == TOTAL_ROOMS,
                              f"{counts['rooms']} (expected {TOTAL_ROOMS})"))
        results.append(Result("room types count", counts["room_types"] == len(ROOM_TYPES),
                              f"{counts['room_types']} (expected {len(ROOM_TYPES)})"))
        results.append(Result("channels count", counts["channels"] == len(CHANNELS),
                              f"{counts['channels']} (expected {len(CHANNELS)})"))
        results.append(Result("guests cover F2's pool", counts["guests"] >= F2_GUEST_POOL,
                              f"{counts['guests']:,} (at least {F2_GUEST_POOL:,})"))
        results.append(Result("cancellation policies (RN-07)", counts["cancellation_policies"] == POLICY_ROWS,
                              f"{counts['cancellation_policies']} rows (expected {POLICY_ROWS})"))
        for table in ("rates", "inventory", "reservations"):
            results.append(Result(f"{table} count", counts[table] > 0, f"{counts[table]:,} rows"))
        results.append(Result("anomalies count", True,
                              f"{counts['reservation_anomalies']:,} rows (injected defects)"))

        # 3b. The database holds exactly the shared catalog (codes other sources join on)
        cur.execute("SELECT code, country, currency, timezone FROM hotels")
        db_hotels = set(cur.fetchall())
        file_hotels = {(h.code, h.country, h.currency, h.timezone) for h in HOTELS}
        cur.execute("SELECT code, channel_type, commission_pct, source FROM channels")
        db_channels = {(c, t, float(p), src) for c, t, p, src in cur.fetchall()}
        file_channels = {(c.code, c.channel_type, float(c.commission_pct), c.source) for c in CHANNELS}
        cur.execute("SELECT code FROM room_types")
        db_types = {r[0] for r in cur.fetchall()}
        diff = (len(db_hotels ^ file_hotels) + len(db_channels ^ file_channels)
                + len(db_types ^ {rt.code for rt in ROOM_TYPES}))
        results.append(Result(f"matches shared catalog {CATALOG_VERSION}", diff == 0,
                              f"{diff} hotel/channel/room-type rows differ" if diff else "hotels, channels and room types identical"))

        # 3c. Guests are the same people F2 events talk about
        cur.execute("SELECT guest_ref, loyalty_tier FROM guests WHERE guest_ref <= %s",
                    (guest_ref(F2_GUEST_POOL),))
        off = sum(1 for ref, tier in cur.fetchall() if tier != loyalty_tier(int(ref[2:])))
        results.append(Result("guest tiers match F2", off == 0,
                              f"{off} guests with another tier than F2" if off else "same rule as F2"))

        # 4. Key relationships and business consistency
        results.append(_zero("rooms -> hotels/room_types", cur, """
            SELECT count(*) FROM rooms r
            LEFT JOIN hotels h ON h.id = r.hotel_id
            LEFT JOIN room_types t ON t.id = r.room_type_id
            WHERE h.id IS NULL OR t.id IS NULL""", "orphan rooms"))
        results.append(_zero("reservations -> parents", cur, """
            SELECT count(*) FROM reservations r
            LEFT JOIN hotels h ON h.id = r.hotel_id
            LEFT JOIN room_types t ON t.id = r.room_type_id
            LEFT JOIN channels c ON c.id = r.channel_id
            LEFT JOIN guests g ON g.id = r.guest_id
            WHERE h.id IS NULL OR t.id IS NULL OR c.id IS NULL OR g.id IS NULL""",
            "orphan reservations"))
        results.append(_zero("inventory total = physical rooms", cur, """
            SELECT count(*) FROM inventory i
            LEFT JOIN (SELECT hotel_id, room_type_id, count(*) AS n
                       FROM rooms WHERE is_active GROUP BY 1, 2) r
              ON r.hotel_id = i.hotel_id AND r.room_type_id = i.room_type_id
            WHERE i.total_rooms <> coalesce(r.n, 0)""", "inventory rows off"))
        results.append(_zero("inventory sold = confirmed bookings", cur, """
            WITH sold AS (
                SELECT r.hotel_id, r.room_type_id, d::date AS night, sum(r.rooms_count) AS n
                FROM reservations r,
                     generate_series(r.check_in, r.check_out - 1, interval '1 day') d
                WHERE r.status = 'confirmed'
                GROUP BY 1, 2, 3)
            SELECT count(*) FROM inventory i
            LEFT JOIN sold s ON s.hotel_id = i.hotel_id AND s.room_type_id = i.room_type_id
                            AND s.night = i.inventory_date
            WHERE i.sold_rooms <> coalesce(s.n, 0)""",
            "inventory rows don't match sales"))
        results.append(_zero("every night has exactly one rate", cur, """
            SELECT count(*) FROM inventory i
            WHERE (SELECT count(*) FROM rates r
                   WHERE r.hotel_id = i.hotel_id AND r.room_type_id = i.room_type_id
                     AND i.inventory_date BETWEEN r.valid_from AND r.valid_to) <> 1""",
            "nights with zero or overlapping rates"))
        results.append(_zero("currency matches hotel", cur, """
            SELECT (SELECT count(*) FROM rates r JOIN hotels h ON h.id = r.hotel_id
                    WHERE r.currency <> h.currency)
                 + (SELECT count(*) FROM reservations r JOIN hotels h ON h.id = r.hotel_id
                    WHERE r.currency <> h.currency)""", "rows in the wrong currency"))
        results.append(_zero("guests fit the rooms", cur, """
            SELECT count(*) FROM reservations r JOIN room_types t ON t.id = r.room_type_id
            WHERE r.adults + r.children > t.max_guests * r.rooms_count""",
            "over-capacity bookings"))
        results.append(_zero("no check-in before the booking day", cur, """
            SELECT count(*) FROM reservations r JOIN hotels h ON h.id = r.hotel_id
            WHERE r.check_in < (r.created_at AT TIME ZONE h.timezone)::date""",
            "bookings for a night that had already passed"))
        results.append(_zero("booking source matches its channel", cur, """
            SELECT count(*) FROM reservations r JOIN channels c ON c.id = r.channel_id
            WHERE r.source <> c.source""",
            "bookings whose source is not their channel's (e.g. an OTA booking marked F3)"))

    return results


def fingerprint(url: str) -> dict[str, str]:
    """md5 of each table's full content, ordered by id. Two runs with the
    same settings must give identical fingerprints: that's the proof the
    seed is reproducible."""
    with connect(url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL TIME ZONE 'UTC'")  # timestamps print the same on any server
        out = {}
        for table in FINGERPRINT_TABLES:
            out[table] = _one(cur, f"SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '-') "
                                   f"FROM {table} t")
        return out


def print_results(results: list[Result]) -> None:
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.name:<{width}}  {r.detail}")
    failed = sum(not r.ok for r in results)
    print(f"\n  {len(results) - failed}/{len(results)} checks passed")
