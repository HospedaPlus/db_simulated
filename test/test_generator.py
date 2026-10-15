"""Generator tests: no database needed."""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta

from seed import catalog
from seed.generator import generate


def test_same_settings_give_identical_data(settings):
    assert generate(settings) == generate(settings)


def test_different_seed_gives_different_data(settings):
    other = generate(replace(settings, seed=7))
    assert other.reservations != generate(settings).reservations
    assert other.hotels == generate(settings).hotels  # catalog never changes


def test_catalog_matches_the_statement(settings):
    ds = generate(settings)
    assert len(ds.hotels) == 28
    assert len(ds.rooms) == 4000
    countries = Counter(h[4] for h in ds.hotels)
    assert countries == {"CO": 26, "PT": 2}  # F1 history = the two Portuguese hotels


def test_codes_match_the_other_simulators(settings):
    # F2/F4/F5 use these exact codes; if this fails, their joins break.
    ds = generate(settings)
    assert [h[0] for h in ds.hotels] == [f"hotel-{i:03d}" for i in range(1, 29)]
    assert [rt[0] for rt in ds.room_types] == ["SINGLE", "DOUBLE", "TWIN", "SUITE"]
    codes = {c[0] for c in ds.channels}
    assert {"booking_channel_a", "booking_channel_b", "expedia", "direct_ota"} <= codes
    assert {f"AG{i:02d}" for i in range(1, 16)} <= codes


def test_commissions_follow_rn02(settings):
    by_type = {c[2]: c[3] for c in generate(settings).channels}
    assert by_type == {"direct": 0, "corporate": 0, "ota": 18, "agency": 10}


def test_no_overbooking(settings):
    ds = generate(settings)
    assert all(0 <= inv[4] <= inv[3] for inv in ds.inventory)


def test_inventory_matches_sold_room_nights(settings):
    ds = generate(settings)
    sold = Counter()
    for r in ds.reservations:
        if r[11] != "cancelled":
            d = r[5]
            while d < r[6]:
                sold[(r[1], r[2], d)] += r[7]
                d += timedelta(days=1)
    for hotel_id, rt_id, day, total, available in ds.inventory:
        assert available == total - sold[(hotel_id, rt_id, day)]


def test_reservations_are_valid(settings):
    ds = generate(settings)
    for r in ds.reservations:
        assert r[6] > r[5]                       # check_out after check_in
        assert r[8] >= 1                         # at least one adult
        assert r[15] >= r[14] >= 0               # total >= first night
        assert (r[11] == "cancelled") == (r[17] is not None)
        assert r[16] <= r[17] if r[17] else True


def test_start_date_moves_the_window(settings):
    a = generate(settings)
    b = generate(replace(settings, start_date=settings.start_date + timedelta(days=1)))
    assert b.window_start == a.window_start + timedelta(days=1)


def test_defect_rates_are_configurable(settings):
    none = generate(replace(settings, zero_guests_rate=0, invalid_dates_rate=0))
    assert none.anomalies == []
    ds = generate(replace(settings, zero_guests_rate=0.05, invalid_dates_rate=0.01))
    kinds = Counter(a[12] for a in ds.anomalies)
    assert kinds["zero_guests"] == round(len(ds.reservations) * 0.05)
    assert kinds["checkout_before_checkin"] == round(len(ds.reservations) * 0.01)
    for a in ds.anomalies:
        if a[12] == "zero_guests":
            assert a[7] == a[8] == a[9] == 0
        else:
            assert a[6] < a[5]


def test_prices_follow_season_multipliers(settings):
    ds = generate(settings)
    # Hotel 1 (Bogota, urban, factor 1.10) DOUBLE in LOW season.
    low = [r for r in ds.rates if r[0] == 1 and r[1] == 2 and r[2] == "LOW"][0]
    expected = round(catalog.BASE_RATES["COP"]["DOUBLE"] * 1.10 * 0.85, -3)
    assert low[6] == expected
    assert date(2026, 10, 14) >= low[3]


def test_no_booking_starts_before_it_was_made(settings):
    from zoneinfo import ZoneInfo

    ds = generate(settings)
    tz = {i: ZoneInfo(h[5]) for i, h in enumerate(ds.hotels, start=1)}
    for r in ds.reservations:
        assert r[5] >= r[16].astimezone(tz[r[1]]).date()
