"""Builds the whole dataset in memory, without touching the database.

Keeping generation pure makes it easy to test and guarantees that the
same settings always give the same rows. Every table gets its own random
stream, so adding guests, say, doesn't reshuffle the reservations.

Ids are not stored in the rows: the loader inserts in list order into
freshly truncated tables, so row N gets id N. That's why rows below
refer to each other by 1-based position.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import catalog
from .config import Settings

TARGET_F3_OCCUPANCY = 0.20   # share of room-nights sold through our own engine
CANCEL_RATE_PAST = 0.37      # statement: 37% of bookings get cancelled
NO_SHOW_RATE_PAST = 0.03
CANCEL_RATE_FUTURE = 0.25    # some future cancellations haven't happened yet
REFUNDABLE_SHARE = 0.75

NIGHTS = {
    "urban":  ([1, 2, 3, 4, 5, 6, 7], [30, 30, 20, 10, 5, 3, 2]),
    "resort": ([2, 3, 4, 5, 6, 7], [15, 25, 25, 15, 10, 10]),
}
LEAD_DAYS = ([0, 3, 7, 14, 30, 60, 120], [8, 12, 15, 20, 20, 15, 10])


@dataclass
class Dataset:
    window_start: date
    window_end: date  # exclusive
    room_types: list = field(default_factory=list)
    hotels: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    guests: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    rates: list = field(default_factory=list)
    inventory: list = field(default_factory=list)
    reservations: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)
    rejected_full: int = 0  # bookings refused because the room type was sold out

    def counts(self) -> dict:
        return {name: len(getattr(self, name)) for name in TABLE_ORDER}


# Load order respects foreign keys; also the order of the TABLE_COLUMNS below.
TABLE_ORDER = [
    "room_types", "hotels", "channels", "guests", "rooms",
    "rates", "inventory", "reservations", "anomalies",
]


def _money(value: float, currency: str) -> float:
    # Pesos are quoted in round thousands; euros keep cents.
    return float(round(value, -3)) if currency == "COP" else round(value, 2)


def _rng(settings: Settings, name: str) -> random.Random:
    return random.Random(f"{settings.seed}:{name}")


def generate(settings: Settings) -> Dataset:
    start = settings.start_date
    window_start = start - timedelta(days=settings.mode.history_days)
    window_end = start + timedelta(days=settings.mode.horizon_days)
    days = [window_start + timedelta(days=i) for i in range((window_end - window_start).days)]
    # The simulation's "now": nothing is created after this moment.
    as_of = datetime.combine(start, time(0, 0), tzinfo=timezone.utc)
    catalog_created = datetime.combine(window_start, time(0, 0), tzinfo=timezone.utc)

    ds = Dataset(window_start, window_end)

    # --- catalogs ----------------------------------------------------------
    rt_index = {}
    for i, rt in enumerate(catalog.ROOM_TYPES, start=1):
        ds.room_types.append((rt.code, rt.name, rt.max_guests))
        rt_index[rt.code] = i

    for h in catalog.HOTELS:
        ds.hotels.append((h.code, h.name, h.category, h.city, h.country,
                          h.timezone, h.currency, True, catalog_created))

    ch_index = {}
    for i, ch in enumerate(catalog.CHANNELS, start=1):
        ds.channels.append((ch.code, ch.name, ch.channel_type, ch.commission_pct))
        ch_index[ch.code] = i

    # --- rooms -------------------------------------------------------------
    # rooms_by_type[(hotel_id, room_type_id)] = how many rooms of that type
    rooms_by_type: dict[tuple[int, int], int] = {}
    for hotel_id, h in enumerate(catalog.HOTELS, start=1):
        mix = catalog.ROOM_MIX[h.category]
        counts = {code: int(h.rooms * share) for code, share in mix.items()}
        counts["DOUBLE"] += h.rooms - sum(counts.values())  # rounding leftovers
        n = 0
        for rt in catalog.ROOM_TYPES:
            rooms_by_type[(hotel_id, rt_index[rt.code])] = counts[rt.code]
            for _ in range(counts[rt.code]):
                floor, slot = divmod(n, 20)  # 20 rooms per floor
                ds.rooms.append((hotel_id, rt_index[rt.code], f"{floor + 1}{slot + 1:02d}", floor + 1, True))
                n += 1

    # --- rates and nightly prices -----------------------------------------
    # price[(hotel_id, room_type_id)][day_index] = nightly rate
    price: dict[tuple[int, int], list[float]] = {}
    for hotel_id, h in enumerate(catalog.HOTELS, start=1):
        base_table = catalog.BASE_RATES[h.currency]
        factor = h.price_factor * (catalog.RESORT_FACTOR if h.category == "resort" else 1.0)
        seasons = [catalog.season_for(d, h.country) for d in days]
        for rt in catalog.ROOM_TYPES:
            key = (hotel_id, rt_index[rt.code])
            nightly = [_money(base_table[rt.code] * factor * catalog.SEASON_MULTIPLIER[s], h.currency)
                       for s in seasons]
            price[key] = nightly
            # Collapse consecutive nights of the same season into one rate row.
            run_start = 0
            for i in range(1, len(days) + 1):
                if i == len(days) or seasons[i] != seasons[run_start]:
                    ds.rates.append((hotel_id, key[1], seasons[run_start], days[run_start],
                                     days[i - 1], h.currency, nightly[run_start]))
                    run_start = i

    # --- guests ------------------------------------------------------------
    rng = _rng(settings, "guests")
    origins = catalog.GUEST_ORIGINS
    tiers = list(catalog.LOYALTY_WEIGHTS)
    tier_w = list(catalog.LOYALTY_WEIGHTS.values())
    for n in range(1, settings.mode.guests + 1):
        country, pool, _ = rng.choices(origins, weights=[o[2] for o in origins])[0]
        first = rng.choice(catalog.NAME_POOLS[pool][0])
        last = rng.choice(catalog.NAME_POOLS[pool][1])
        email = f"{first}.{last}{n}@example.com".lower()  # example.com never delivers mail
        ds.guests.append((f"{first} {last}", email, country, rng.choices(tiers, tier_w)[0]))

    # --- reservations ------------------------------------------------------
    rng = _rng(settings, "reservations")
    available = {key: [total] * len(days) for key, total in rooms_by_type.items()}
    max_guests = {rt_index[rt.code]: rt.max_guests for rt in catalog.ROOM_TYPES}
    hotel_ids = list(range(1, len(catalog.HOTELS) + 1))
    hotel_w = [h.rooms for h in catalog.HOTELS]
    f3_channels = list(catalog.F3_CHANNEL_WEIGHTS)
    f3_w = list(catalog.F3_CHANNEL_WEIGHTS.values())

    room_nights = catalog.TOTAL_ROOMS * len(days)
    attempts = int(room_nights * TARGET_F3_OCCUPANCY / (2.9 * (1 - CANCEL_RATE_PAST)))

    for _ in range(attempts):
        hotel_id = rng.choices(hotel_ids, hotel_w)[0]
        h = catalog.HOTELS[hotel_id - 1]
        rt_ids = [rt_index[rt.code] for rt in catalog.ROOM_TYPES]
        rt_id = rng.choices(rt_ids, [rooms_by_type[(hotel_id, r)] for r in rt_ids])[0]
        nights = rng.choices(*NIGHTS[h.category])[0]
        if nights >= len(days):
            continue
        first_idx = rng.randrange(0, len(days) - nights)
        check_in = days[first_idx]
        check_out = check_in + timedelta(days=nights)
        rooms_count = 2 if rng.random() < 0.08 else 1

        capacity = max_guests[rt_id] * rooms_count
        adults = rng.randint(1, capacity)
        children = rng.randint(0, capacity - adults) if adults < capacity and rng.random() < 0.4 else 0
        infants = 1 if rng.random() < 0.05 else 0

        channel = rng.choices(f3_channels, f3_w)[0]
        guest_id = rng.randint(1, len(ds.guests))

        # Booked `lead` days before arrival, at some minute of that day, in the
        # hotel's own time zone: nobody can book a night that already passed.
        lead = rng.choices(*LEAD_DAYS)[0] + rng.randint(0, 6)
        created = (datetime.combine(check_in - timedelta(days=lead), time(0, 0), tzinfo=ZoneInfo(h.timezone))
                   + timedelta(minutes=rng.randint(0, 24 * 60 - 1))).astimezone(timezone.utc)
        if created > as_of:
            created = as_of - timedelta(minutes=rng.randint(1, 7 * 24 * 60))

        # Status depends on whether the stay is already in the past.
        roll = rng.random()
        if check_in < start:
            status = ("cancelled" if roll < CANCEL_RATE_PAST
                      else "no_show" if roll < CANCEL_RATE_PAST + NO_SHOW_RATE_PAST
                      else "confirmed")
        else:
            status = "cancelled" if roll < CANCEL_RATE_FUTURE else "confirmed"

        cancelled_at = None
        if status == "cancelled":
            latest = min(datetime.combine(check_in, time(0, 0), tzinfo=timezone.utc), as_of)
            span = max(int((latest - created).total_seconds()), 60)
            cancelled_at = created + timedelta(seconds=rng.randint(60, span) if span > 60 else 60)

        # No overbooking: a booking that would take a room we don't have is refused.
        nights_idx = range(first_idx, first_idx + nights)
        slots = available[(hotel_id, rt_id)]
        if status != "cancelled":
            if any(slots[i] < rooms_count for i in nights_idx):
                ds.rejected_full += 1
                continue
            for i in nights_idx:
                slots[i] -= rooms_count

        nightly = price[(hotel_id, rt_id)]
        discount = catalog.CORPORATE_DISCOUNT if channel == "corporate" else 1.0
        per_night = [_money(nightly[i] * discount, h.currency) for i in nights_idx]
        first_night = _money(per_night[0] * rooms_count, h.currency)
        total = _money(sum(per_night) * rooms_count, h.currency)
        refundable = channel == "corporate" or rng.random() < REFUNDABLE_SHARE

        code = f"F3-{len(ds.reservations) + 1:07d}"
        ds.reservations.append((code, hotel_id, rt_id, ch_index[channel], guest_id,
                                check_in, check_out, rooms_count, adults, children, infants,
                                status, refundable, h.currency, first_night, total,
                                created, cancelled_at))

    # --- inventory (derived from what was actually sold) ------------------
    for (hotel_id, rt_id), total_rooms in rooms_by_type.items():
        slots = available[(hotel_id, rt_id)]
        for i, d in enumerate(days):
            ds.inventory.append((hotel_id, rt_id, d, total_rooms, slots[i]))

    # --- injected defects (statement 5.3 / US-26) -------------------------
    rng = _rng(settings, "anomalies")
    n_zero = round(len(ds.reservations) * settings.zero_guests_rate)
    n_dates = round(len(ds.reservations) * settings.invalid_dates_rate)
    for n, defect in enumerate(["zero_guests"] * n_zero + ["checkout_before_checkin"] * n_dates, start=1):
        src = ds.reservations[rng.randrange(len(ds.reservations))]
        check_in, check_out = src[5], src[6]
        adults, children, infants = src[8], src[9], src[10]
        if defect == "zero_guests":
            adults = children = infants = 0
        else:
            check_out = check_in - timedelta(days=rng.randint(1, 3))
        ds.anomalies.append((f"F3X-{n:06d}", src[1], src[2], src[3], src[4], check_in, check_out,
                             adults, children, infants, src[13], src[15], defect, src[16]))

    return ds


# Column lists used by the loader, one per table, in the tuple order above.
TABLE_COLUMNS = {
    "room_types": ("room_types", ["code", "name", "max_guests"]),
    "hotels": ("hotels", ["code", "name", "category", "city", "country", "timezone",
                          "currency", "is_active", "created_at"]),
    "channels": ("channels", ["code", "name", "channel_type", "commission_pct"]),
    "guests": ("guests", ["full_name", "email", "country", "loyalty_tier"]),
    "rooms": ("rooms", ["hotel_id", "room_type_id", "room_number", "floor", "is_active"]),
    "rates": ("rates", ["hotel_id", "room_type_id", "season_code", "valid_from", "valid_to",
                        "currency", "amount"]),
    "inventory": ("inventory", ["hotel_id", "room_type_id", "inventory_date", "total_rooms",
                                "available_rooms"]),
    "reservations": ("reservations", ["code", "hotel_id", "room_type_id", "channel_id", "guest_id",
                                      "check_in", "check_out", "rooms_count", "adults", "children",
                                      "infants", "status", "is_refundable", "currency",
                                      "first_night_amount", "total_amount", "created_at",
                                      "cancelled_at"]),
    "anomalies": ("reservation_anomalies", ["code", "hotel_id", "room_type_id", "channel_id",
                                            "guest_id", "check_in", "check_out", "adults",
                                            "children", "infants", "currency", "total_amount",
                                            "defect_type", "created_at"]),
}
