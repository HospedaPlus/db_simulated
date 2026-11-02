"""Continuous F3 source: keeps direct and corporate bookings arriving.

The statement treats F3 as a live source, so after the seed this process
keeps the booking engine "open": every few seconds a new direct or corporate
booking goes through operation.reservar() (the same function the API, the F2
consumer and the agencies DAG use), and now and then a guest cancels through
operation.cancelar(). Once a day it adds tomorrow's night 365 days ahead to
inventory and rates, so there is always a year to sell.

It only adds rows, never deletes, so it runs against Neon without
--allow-remote. Use the app_writer role, like the real API would.

    python -m seed simulate                       # 6 bookings a minute, until Ctrl+C
    python -m seed simulate --per-minute 60 --duration 120
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from . import catalog
from .generator import LEAD_DAYS, NIGHTS, REFUNDABLE_SHARE, nightly_price
from .safety import redact

HORIZON_DAYS = 365  # nights ahead that must always be on sale
CANCEL_SHARE = 0.15  # share of the actions that are cancellations


def extend_calendar(conn: psycopg.Connection, until: date) -> int:
    """Add inventory (0 sold) and one rate row per night up to `until`, for
    every hotel and room type that doesn't reach it yet. Returns the nights added."""
    hotels = {code: hid for hid, code in conn.execute("SELECT id, code FROM operation.hotels")}
    types = {code: tid for tid, code in conn.execute("SELECT id, code FROM operation.room_types")}
    rooms = {(h, t): n for h, t, n in conn.execute(
        "SELECT hotel_id, room_type_id, count(*) FROM operation.rooms WHERE is_active GROUP BY 1, 2")}
    last = {(h, t): d for h, t, d in conn.execute(
        "SELECT hotel_id, room_type_id, max(inventory_date) FROM operation.inventory GROUP BY 1, 2")}

    inventory_rows, rate_rows = [], []
    for h in catalog.HOTELS:
        for rt in catalog.ROOM_TYPES:
            key = (hotels[h.code], types[rt.code])
            day = (last.get(key) or date.today() - timedelta(days=1)) + timedelta(days=1)
            while day <= until:
                season = catalog.season_for(day, h.country)
                inventory_rows.append((*key, day, rooms.get(key, 0)))
                rate_rows.append((*key, season, day, day, h.currency, nightly_price(h, rt.code, season)))
                day += timedelta(days=1)

    if inventory_rows:
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("INSERT INTO operation.inventory (hotel_id, room_type_id, inventory_date, "
                            "total_rooms) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", inventory_rows)
            cur.executemany("INSERT INTO operation.rates (hotel_id, room_type_id, season_code, valid_from, "
                            "valid_to, currency, amount) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING", rate_rows)
    return len(inventory_rows)


def book_one(conn: psycopg.Connection, rng: random.Random) -> str:
    """One new direct or corporate booking, with the same habits as the seed."""
    h = rng.choices(catalog.HOTELS, [x.rooms for x in catalog.HOTELS])[0]
    channels = catalog.F3_CHANNEL_WEIGHTS[h.category]
    channel = rng.choices(list(channels), list(channels.values()))[0]
    segment = "corporate" if channel == "corporate" else h.category
    mix = catalog.ROOM_MIX[h.category]
    room_type = rng.choices(list(mix), list(mix.values()))[0]
    max_guests = next(rt.max_guests for rt in catalog.ROOM_TYPES if rt.code == room_type)

    today_local = datetime.now(ZoneInfo(h.timezone)).date()  # "today" where the hotel is
    lead = rng.choices(*LEAD_DAYS[segment])[0] + rng.randint(0, 6)
    nights = rng.choices(*NIGHTS[segment])[0]
    check_in = today_local + timedelta(days=lead)
    adults = rng.randint(1, max_guests)
    refundable = channel == "corporate" or rng.random() < REFUNDABLE_SHARE
    code = f"F3-S{uuid.uuid4().hex[:10].upper()}"
    guest = catalog.guest_ref(rng.randint(1, catalog.F2_GUEST_POOL))

    result, res_code, _id, full = conn.execute(
        "SELECT * FROM operation.reservar(%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)",
        (channel, code, h.code, room_type, guest, check_in, check_in + timedelta(days=nights),
         adults, refundable)).fetchone()
    detail = f"{h.code} {room_type} {check_in} x{nights}n {channel}"
    if result == "rejected":
        return f"rejected  {detail} (no room: {', '.join(str(d) for d in full) or 'no inventory'})"
    return f"{result:<9} {res_code} {detail}"


def cancel_one(conn: psycopg.Connection, rng: random.Random) -> str | None:
    """A guest cancels one of the latest future F3 bookings (penalty per RN-07)."""
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM operation.reservations WHERE source = 'F3' AND status = 'confirmed' "
        "AND check_in > current_date ORDER BY id DESC LIMIT 50")]
    if not codes:
        return None
    result, code, hours, pct, amount, currency, _rule = conn.execute(
        "SELECT * FROM operation.cancelar(%s)", (rng.choice(codes),)).fetchone()
    return f"{result:<9} {code} {hours} h before check-in, penalty {pct}% = {amount} {currency}"


def inject_defect(conn: psycopg.Connection, rng: random.Random, zero_guests_rate: float,
                  invalid_dates_rate: float) -> str | None:
    """Statement 5.3: now and then copy the latest F3 booking into
    reservation_anomalies with zero guests or check-out before check-in,
    like the seed does, so the continuous source keeps sending dirty rows."""
    roll = rng.random()
    if roll < zero_guests_rate:
        defect = "zero_guests"
    elif roll < zero_guests_rate + invalid_dates_rate:
        defect = "checkout_before_checkin"
    else:
        return None
    row = conn.execute(
        "SELECT hotel_id, room_type_id, channel_id, guest_id, check_in, check_out, adults, children, "
        "infants, currency, total_amount FROM operation.reservations WHERE source = 'F3' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    hotel, rt, channel, guest, check_in, check_out, adults, children, infants, currency, total = row
    if defect == "zero_guests":
        adults = children = infants = 0
    else:
        check_out = check_in - timedelta(days=rng.randint(1, 3))
    code = f"F3X-S{uuid.uuid4().hex[:10].upper()}"
    conn.execute(
        "INSERT INTO operation.reservation_anomalies (code, hotel_id, room_type_id, channel_id, guest_id, "
        "check_in, check_out, adults, children, infants, currency, total_amount, defect_type) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (code, hotel, rt, channel, guest, check_in, check_out, adults, children, infants, currency,
         total, defect))
    return f"anomaly   {code} {defect}"


def run(url: str, per_minute: float, duration: float = 0, cancel_share: float = CANCEL_SHARE,
        seed: int | None = None, zero_guests_rate: float = 0.02,
        invalid_dates_rate: float = 0.02) -> dict:
    """Loop until Ctrl+C (or `duration` seconds). Returns a count per result."""
    rng = random.Random(seed)
    pause = 60.0 / per_minute
    stats: dict[str, int] = {}
    extended_on = None
    end = time.monotonic() + duration if duration else None
    print(f"Simulating F3 bookings on {redact(url)}: {per_minute:g}/min, Ctrl+C to stop", flush=True)
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        try:
            while end is None or time.monotonic() < end:
                if extended_on != date.today():
                    added = extend_calendar(conn, date.today() + timedelta(days=HORIZON_DAYS))
                    print(f"calendar  +{added} hotel/room-type nights on sale", flush=True)
                    extended_on = date.today()
                line = cancel_one(conn, rng) if rng.random() < cancel_share else None
                line = line or book_one(conn, rng)
                key = line.split()[0]
                stats[key] = stats.get(key, 0) + 1
                print(f"{datetime.now():%H:%M:%S} {line}", flush=True)
                defect = inject_defect(conn, rng, zero_guests_rate, invalid_dates_rate)
                if defect:
                    stats["anomaly"] = stats.get("anomaly", 0) + 1
                    print(f"{datetime.now():%H:%M:%S} {defect}", flush=True)
                time.sleep(pause)
        except KeyboardInterrupt:
            pass
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())), flush=True)
    return stats
