"""reservar(), liberar(), penalidad(), cancelar() and the overbooking limits,
against a real PostgreSQL (TEST_DATABASE_URL). SQLite would not do: the
concurrency tests depend on SELECT ... FOR UPDATE."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from seed import simulate

from .conftest import load_fresh

TODAY = date.today()


@pytest.fixture(scope="module")
def url(db_url):
    load_fresh(db_url)  # start date = today, so the next 365 nights are on sale
    return db_url


def q(url, sql, params=()):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount


def book(url, external, hotel="HP-CO-001", room_type="DOUBLE", check_in=None, nights=1,
         channel="direct_web", refundable=True, guest="G-0000001"):
    check_in = check_in or TODAY + timedelta(days=30)
    return q(url, "SELECT * FROM operation.reservar(%s, %s, %s, %s, %s, %s, %s, 1, 1, %s)",
             (channel, external, hotel, room_type, guest, check_in,
              check_in + timedelta(days=nights), refundable))[0]


def set_night(url, hotel, room_type, night, free, limit=0):
    """Leave exactly `free` physical rooms unsold that night, with this overbooking limit."""
    q(url, """UPDATE operation.inventory i SET sold_rooms = total_rooms - %s, overbooking_limit = %s
              FROM operation.hotels h, operation.room_types t
              WHERE h.id = i.hotel_id AND t.id = i.room_type_id
                AND h.code = %s AND t.code = %s AND i.inventory_date = %s""",
      (free, limit, hotel, room_type, night))


def race(url, prefix, hotel, night, buyers=50):
    """`buyers` concurrent requests for the same room type and night."""
    def one(i):
        return book(url, f"{prefix}-{i}", hotel=hotel, check_in=night)[0]

    with ThreadPoolExecutor(max_workers=buyers) as pool:
        results = list(pool.map(one, range(buyers)))
    rejected = q(url, "SELECT count(*) FROM operation.reservation_rejections WHERE external_code LIKE %s",
                 (f"{prefix}-%",))[0][0]
    return results.count("confirmed"), results.count("rejected"), rejected


def test_last_room_is_sold_once_under_concurrency(url):
    """CA-08 / E-15: 50 buyers, 1 room left, limit 0 -> 1 sale, 49 rejections stored (RN-04)."""
    night = TODAY + timedelta(days=40)
    set_night(url, "HP-MX-001", "DOUBLE", night, free=1, limit=0)
    assert race(url, "RACE0", "HP-MX-001", night) == (1, 49, 49)
    assert q(url, """SELECT available_rooms FROM operation.inventory i JOIN operation.hotels h ON h.id = i.hotel_id
                     WHERE h.code = 'HP-MX-001' AND i.room_type_id = 2 AND i.inventory_date = %s""",
             (night,))[0][0] == 0


def test_overbooking_limit_allows_exactly_that_many_more(url):
    """CA-08: 1 room left + limit 2 -> exactly 3 sales."""
    night = TODAY + timedelta(days=41)
    set_night(url, "HP-BR-001", "DOUBLE", night, free=1, limit=2)
    confirmed, rejected, stored = race(url, "RACE2", "HP-BR-001", night)
    assert (confirmed, rejected, stored) == (3, 47, 47)


def test_same_booking_twice_is_stored_once(url):
    """RNF-05: a redelivered event or a re-run DAG doesn't book twice."""
    first = book(url, "IDEM-1", hotel="HP-ES-001")
    again = book(url, "IDEM-1", hotel="HP-ES-001")
    assert first[0] == "confirmed" and again[0] == "already_exists"
    assert again[1] == first[1]
    assert q(url, "SELECT count(*) FROM operation.reservations WHERE external_code = 'IDEM-1'")[0][0] == 1


def test_ota_booking_gets_its_source_and_rate(url):
    """F2 sends its own nightly rate; the code is prefixed with the source."""
    row = q(url, """SELECT * FROM operation.reservar('expedia', 'abc-1', 'HP-PT-001', 'SUITE', 'G-0000021',
                    %s, %s, 1, 2, false, 0, 0, 150)""",
            (TODAY + timedelta(days=5), TODAY + timedelta(days=7)))[0]
    assert row[:2] == ("confirmed", "F2-abc-1")
    assert q(url, "SELECT source, first_night_amount, total_amount FROM operation.reservations "
                  "WHERE code = 'F2-abc-1'")[0] == ("F2", Decimal("150.00"), Decimal("300.00"))


def test_nights_not_on_sale_are_rejected(url):
    result = book(url, "FAR-1", check_in=TODAY + timedelta(days=500))
    assert result[0] == "rejected"
    assert q(url, "SELECT reason FROM operation.reservation_rejections WHERE external_code = 'FAR-1'") \
        == [("no_inventory",)]


def test_unknown_codes_raise(url):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        book(url, "BAD-1", hotel="HP-XX-999")


def test_high_event_sets_limit_to_zero(url):
    """CA-02: a 'high' local event (F6) means no overbooking at all that night."""
    night = TODAY + timedelta(days=20)  # lead 20 days -> bucket 8-30
    set_night(url, "HP-CO-002", "DOUBLE", night, free=0, limit=0)
    q(url, "INSERT INTO operation.cancellation_rate_by_lead VALUES ('HP-CO-002', '8-30', 0.40, now())")
    limit_sql = """SELECT overbooking_limit FROM operation.inventory i
                   JOIN operation.hotels h ON h.id = i.hotel_id
                   WHERE h.code = 'HP-CO-002' AND i.room_type_id = 2 AND i.inventory_date = %s"""

    q(url, "SELECT operation.refresh_overbooking_limits(%s)", (TODAY,))
    assert q(url, limit_sql, (night,))[0][0] > 0          # cancellations expected -> some overbooking

    q(url, """INSERT INTO operation.local_events (hotel_code, name, start_date, end_date, category)
              VALUES ('HP-CO-002', 'Festival', %s, %s, 'high')""", (night, night))
    q(url, "SELECT operation.refresh_overbooking_limits(%s)", (TODAY,))
    assert q(url, limit_sql, (night,))[0][0] == 0


def _penalty_at(url, code, hours_before, tz="America/Bogota"):
    check_in = q(url, "SELECT check_in FROM operation.reservations WHERE code = %s", (code,))[0][0]
    at = datetime.combine(check_in, time(15, 0), tzinfo=ZoneInfo(tz)) - timedelta(hours=hours_before)
    return q(url, "SELECT penalty_pct, penalty_amount FROM operation.penalidad(%s, %s)",
             (code, at.astimezone(timezone.utc)))[0]


def test_rn07_penalties(url):
    """CA-05: 48 h before a refundable stay -> 50% of the first night, etc."""
    book(url, "PEN-R", hotel="HP-CO-003", nights=3)
    book(url, "PEN-N", hotel="HP-CO-003", nights=3, refundable=False)
    first, total = q(url, "SELECT first_night_amount, total_amount FROM operation.reservations "
                          "WHERE code = 'PEN-R'")[0]
    assert _penalty_at(url, "PEN-R", 100) == (Decimal("0.00"), Decimal("0"))
    assert _penalty_at(url, "PEN-R", 48) == (Decimal("50.00"), round(first / 2, -3))
    assert _penalty_at(url, "PEN-R", 10)[0] == Decimal("100.00")
    assert _penalty_at(url, "PEN-R", -5)[0] == Decimal("100.00")   # no-show
    pct, amount = _penalty_at(url, "PEN-N", 200)
    assert pct == Decimal("100.00")
    total_n = q(url, "SELECT total_amount FROM operation.reservations WHERE code = 'PEN-N'")[0][0]
    assert amount == total_n


def test_cancel_frees_the_nights_once(url):
    night = TODAY + timedelta(days=60)
    sold_sql = """SELECT sold_rooms FROM operation.inventory i JOIN operation.hotels h ON h.id = i.hotel_id
                  WHERE h.code = 'HP-PT-003' AND i.room_type_id = 2 AND i.inventory_date = %s"""
    before = q(url, sold_sql, (night,))[0][0]
    book(url, "CAN-1", hotel="HP-PT-003", check_in=night)
    assert q(url, sold_sql, (night,))[0][0] == before + 1

    result = q(url, "SELECT result, penalty_pct FROM operation.cancelar('CAN-1')")[0]
    assert result == ("cancelled", Decimal("0.00"))     # 60 days ahead: free
    assert q(url, sold_sql, (night,))[0][0] == before
    assert q(url, "SELECT result FROM operation.cancelar('CAN-1')")[0][0] == "not_cancellable"
    assert q(url, sold_sql, (night,))[0][0] == before
    assert q(url, "SELECT status, penalty_amount FROM operation.reservations WHERE code = 'CAN-1'")[0] \
        == ("cancelled", Decimal("0.00"))


def test_simulator_keeps_bookings_coming(url):
    stats = simulate.run(url, per_minute=6000, duration=2, seed=1)
    assert stats.get("confirmed", 0) > 0
    with psycopg.connect(url, autocommit=True) as conn:
        assert simulate.extend_calendar(conn, TODAY + timedelta(days=simulate.HORIZON_DAYS)) == 0
        assert simulate.extend_calendar(conn, TODAY + timedelta(days=simulate.HORIZON_DAYS + 1)) == 112


def _sold(url, hotel, room_type, night):
    return q(url, """SELECT sold_rooms FROM operation.inventory i
                     JOIN operation.hotels h ON h.id = i.hotel_id
                     JOIN operation.room_types t ON t.id = i.room_type_id
                     WHERE h.code = %s AND t.code = %s AND i.inventory_date = %s""",
             (hotel, room_type, night))[0][0]


def _modify(url, external, **kw):
    args = dict(room_type=None, check_in=None, check_out=None, occurred_at=None)
    args.update(kw)
    return q(url, """SELECT * FROM operation.modificar('direct_web', %s, %s, %s, %s,
                     NULL, NULL, NULL, NULL, NULL, coalesce(%s, now()))""",
             (external, args["room_type"], args["check_in"], args["check_out"], args["occurred_at"]))[0]


def test_modify_moves_the_nights(url):
    """New dates: the old nights are freed and the new ones taken, in one step."""
    old, new = TODAY + timedelta(days=70), TODAY + timedelta(days=80)
    before_old, before_new = _sold(url, "HP-MX-002", "DOUBLE", old), _sold(url, "HP-MX-002", "DOUBLE", new)
    book(url, "MOD-1", hotel="HP-MX-002", check_in=old, nights=1)
    result = _modify(url, "MOD-1", check_in=new, check_out=new + timedelta(days=1))
    assert result[0] == "modified"
    assert _sold(url, "HP-MX-002", "DOUBLE", old) == before_old
    assert _sold(url, "HP-MX-002", "DOUBLE", new) == before_new + 1
    assert q(url, "SELECT check_in FROM operation.reservations WHERE code = 'MOD-1'")[0][0] == new


def test_modify_without_room_changes_nothing(url):
    """New stay is full: rejected, stored in rejections, booking and inventory as they were."""
    old, full = TODAY + timedelta(days=90), TODAY + timedelta(days=91)
    book(url, "MOD-2", hotel="HP-MX-003", check_in=old, nights=1)
    set_night(url, "HP-MX-003", "DOUBLE", full, free=0, limit=0)
    sold_old = _sold(url, "HP-MX-003", "DOUBLE", old)
    result = _modify(url, "MOD-2", check_in=full, check_out=full + timedelta(days=1))
    assert result[0] == "rejected" and result[2] == [full]
    assert _sold(url, "HP-MX-003", "DOUBLE", old) == sold_old
    assert q(url, "SELECT check_in FROM operation.reservations WHERE code = 'MOD-2'")[0][0] == old
    assert q(url, "SELECT count(*) FROM operation.reservation_rejections WHERE external_code = 'MOD-2'")[0][0] == 1


def test_modify_room_type_on_same_nights(url):
    night = TODAY + timedelta(days=95)
    book(url, "MOD-3", hotel="HP-BR-002", check_in=night, nights=2)
    double, suite = _sold(url, "HP-BR-002", "DOUBLE", night), _sold(url, "HP-BR-002", "SUITE", night)
    assert _modify(url, "MOD-3", room_type="SUITE")[0] == "modified"
    assert _sold(url, "HP-BR-002", "DOUBLE", night) == double - 1
    assert _sold(url, "HP-BR-002", "SUITE", night) == suite + 1


def test_modify_edge_cases(url):
    assert _modify(url, "NEVER-CREATED")[0] == "not_found"        # arrived before 'created'
    book(url, "MOD-4", hotel="HP-ES-003")
    t1 = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert _modify(url, "MOD-4", occurred_at=t1)[0] == "modified"
    assert _modify(url, "MOD-4", occurred_at=t1 - timedelta(seconds=30))[0] == "stale"  # older event
    q(url, "SELECT operation.cancelar('MOD-4')")
    assert _modify(url, "MOD-4", occurred_at=t1 + timedelta(minutes=1))[0] == "not_modifiable"


def test_simulator_injects_defects(url):
    stats = simulate.run(url, per_minute=6000, duration=1, seed=2,
                         zero_guests_rate=0.5, invalid_dates_rate=0.5)
    assert stats.get("anomaly", 0) > 0
    with psycopg.connect(url) as conn:
        bad = conn.execute("SELECT count(*) FROM operation.reservation_anomalies WHERE code LIKE 'F3X-S%' "
                           "AND NOT ((adults + children + infants = 0) OR check_out < check_in)").fetchone()[0]
    assert bad == 0
