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
    assert countries == {"CO": 10, "MX": 6, "BR": 6, "PT": 3, "ES": 3}


def test_hotels_come_from_the_shared_catalog(settings):
    # F2/F4/F5 read the same catalog.json; if this fails, their joins break.
    import json

    shared = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    ds = generate(settings)
    assert [h[0] for h in ds.hotels] == [h["code"] for h in shared["hotels"]]
    assert [rt[0] for rt in ds.room_types] == ["SINGLE", "DOUBLE", "TWIN", "SUITE"]
    assert {c[0] for c in ds.channels} == {c["code"] for c in shared["channels"]}
    by_code = {h["code"]: h for h in shared["hotels"]}
    for code, _name, _cat, _city, country, tz, currency, *_ in ds.hotels:
        assert (country, tz, currency) == (by_code[code]["country"], by_code[code]["timezone"],
                                           by_code[code]["currency"])


def test_f1_hotels_are_the_portuguese_pair(settings):
    # The Kaggle history (F1) belongs to one city hotel and one resort in Portugal.
    import json

    shared = json.loads(catalog.CATALOG_PATH.read_text(encoding="utf-8"))
    f1 = {h["code"]: h["f1_profile"] for h in shared["hotels"] if "f1_profile" in h}
    assert f1 == {"HP-PT-001": "City Hotel", "HP-PT-002": "Resort Hotel"}


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
    # HP-CO-005 (Bogota, urban, factor 1.10) DOUBLE in LOW season (CO: Sep-Nov).
    hotel_id = [h[0] for h in ds.hotels].index("HP-CO-005") + 1
    low = [r for r in ds.rates if r[0] == hotel_id and r[1] == 2 and r[2] == "LOW"][0]
    expected = round(catalog.BASE_RATES["COP"]["DOUBLE"] * 1.10 * 0.85, -3)
    assert low[6] == expected
    assert date(2026, 10, 14) >= low[3]


def test_every_hotel_sells_in_its_country_currency(settings):
    ds = generate(settings)
    currency = {"CO": "COP", "MX": "MXN", "BR": "BRL", "PT": "EUR", "ES": "EUR"}
    assert all(h[6] == currency[h[4]] for h in ds.hotels)


def test_seasons_follow_each_country(settings):
    # Brazil is in the southern hemisphere: January is high season there,
    # while in Spain it is low season after Epiphany.
    assert catalog.season_for(date(2027, 1, 20), "BR") == "HIGH"
    assert catalog.season_for(date(2027, 1, 20), "ES") == "LOW"
    assert catalog.season_for(date(2026, 12, 31), "CO") == "HIGH"
    assert catalog.season_for(date(2026, 10, 1), "MX") == "LOW"
    assert catalog.season_for(date(2026, 5, 10), "PT") == "MID"


def test_no_booking_starts_before_it_was_made(settings):
    from zoneinfo import ZoneInfo

    ds = generate(settings)
    tz = {i: ZoneInfo(h[5]) for i, h in enumerate(ds.hotels, start=1)}
    for r in ds.reservations:
        assert r[5] >= r[16].astimezone(tz[r[1]]).date()
