"""Health and integrity checks (acceptance criterion 4).

Each check returns PASS/FAIL plus a short detail, so the output can be
pasted into a ticket as evidence. Run with `python -m seed check`.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .catalog import CHANNELS, HOTELS, ROOM_TYPES, TOTAL_ROOMS
from .db import connect

REQUIRED_TABLES = [
    "room_types", "hotels", "channels", "guests", "rooms",
    "rates", "inventory", "reservations", "reservation_anomalies",
]
FINGERPRINT_TABLES = REQUIRED_TABLES


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
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        present = {r[0] for r in cur.fetchall()}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        results.append(Result("required tables", not missing,
                              f"missing: {', '.join(missing)}" if missing
                              else f"all {len(REQUIRED_TABLES)} present"))
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
        for table in ("guests", "rates", "inventory", "reservations"):
            results.append(Result(f"{table} count", counts[table] > 0, f"{counts[table]:,} rows"))
        results.append(Result("anomalies count", True,
                              f"{counts['reservation_anomalies']:,} rows (injected defects)"))

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
        results.append(_zero("inventory available = total - sold", cur, """
            WITH sold AS (
                SELECT r.hotel_id, r.room_type_id, d::date AS night, sum(r.rooms_count) AS n
                FROM reservations r,
                     generate_series(r.check_in, r.check_out - 1, interval '1 day') d
                WHERE r.status <> 'cancelled'
                GROUP BY 1, 2, 3)
            SELECT count(*) FROM inventory i
            LEFT JOIN sold s ON s.hotel_id = i.hotel_id AND s.room_type_id = i.room_type_id
                            AND s.night = i.inventory_date
            WHERE i.available_rooms <> i.total_rooms - coalesce(s.n, 0)""",
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
        results.append(_zero("F3 bookings use F3 channels", cur, """
            SELECT count(*) FROM reservations r JOIN channels c ON c.id = r.channel_id
            WHERE c.channel_type NOT IN ('direct', 'corporate')""",
            "OTA/agency bookings in F3"))

    return results


def fingerprint(url: str) -> dict[str, str]:
    """md5 of each table's full content, ordered by id. Two runs with the
    same settings must give identical fingerprints: that's the proof the
    seed is reproducible."""
    with connect(url) as conn, conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")  # timestamps print the same on any server
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
