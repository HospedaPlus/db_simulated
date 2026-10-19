/*Creation of tables room_type, hotels, room, rates, inventory ,channels, guests, reservation*/

BEGIN;


CREATE TABLE IF NOT EXISTS room_types (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(20) NOT NULL,           -- SINGLE, DOUBLE, TWIN, SUITE
    name VARCHAR(60) NOT NULL,
    max_guests INTEGER NOT NULL,           -- per room, infants not counted
    CONSTRAINT uq_room_types_code UNIQUE (code),
    CONSTRAINT ck_room_types_max_guests CHECK (max_guests > 0)
);

CREATE TABLE IF NOT EXISTS hotels (
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(12)  NOT NULL,          -- HP-CO-001 ... (shared catalog)
    name  VARCHAR(120) NOT NULL,
    category VARCHAR(10)  NOT NULL,          -- urban or resort (statement: urban + vacation hotels)
    city  VARCHAR(80)  NOT NULL,
    country CHAR(2)      NOT NULL,          -- ISO 3166-1, drives F6 holidays
    timezone VARCHAR(64)  NOT NULL,          -- prices go out before 06:00 local time
    currency CHAR(3) NOT NULL,          -- the currency this hotel sells in
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hotels_code  UNIQUE (code),
    CONSTRAINT ck_hotels_category CHECK (category IN ('urban', 'resort')),
    CONSTRAINT ck_hotels_currency CHECK (currency ~ '^[A-Z]{3}$')
);

-- Commission per channel comes from RN-02.
CREATE TABLE IF NOT EXISTS channels (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(30)  NOT NULL,
    name  VARCHAR(80)  NOT NULL,
    channel_type    VARCHAR(10)  NOT NULL,
    commission_pct  NUMERIC(5,2) NOT NULL DEFAULT 0,
    CONSTRAINT uq_channels_code UNIQUE (code),
    CONSTRAINT ck_channels_type CHECK (channel_type IN ('direct', 'ota', 'agency', 'corporate')),
    CONSTRAINT ck_channels_commission_pct CHECK (commission_pct BETWEEN 0 AND 100)
);

-- Personal data lives only here (RN-11: pseudonymize before analytics).
CREATE TABLE IF NOT EXISTS guests (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name  VARCHAR(120) NOT NULL,
    email  VARCHAR(180),
    country  CHAR(2),
    loyalty_tier  VARCHAR(10)  NOT NULL DEFAULT 'none',   -- gold guests are never relocated (RN-09)
    CONSTRAINT ck_guests_loyalty_tier CHECK (loyalty_tier IN ('none', 'silver', 'gold'))
);

/*Hotel detail tables.
A hotel owns its rooms, rates and inventory, so they go away
with it (CASCADE). Room types are shared, so they can't be
deleted while something still points at them (RESTRICT). */

CREATE TABLE IF NOT EXISTS rooms (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_id  INT NOT NULL REFERENCES hotels(id)     ON DELETE CASCADE,
    room_type_id  INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    room_number   VARCHAR(10) NOT NULL,
    floor INTEGER NOT NULL,
    is_active BOOLEAN  NOT NULL DEFAULT TRUE,
    CONSTRAINT uq_rooms_hotel_number UNIQUE (hotel_id, room_number),
    CONSTRAINT ck_rooms_floor CHECK (floor >= 0)
);

-- One row per hotel, room type and season period.
-- amount = base rate x season multiplier (RN-05, values in catalog/catalog.json).
-- Pickup and event multipliers (RN-05/RN-06) are applied later by the pricing service.*/
CREATE TABLE IF NOT EXISTS rates (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_id INT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_type_id INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    season_code VARCHAR(10) NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE NOT NULL,       -- inclusive
    currency CHAR(3) NOT NULL,
    amount  NUMERIC(12,2) NOT NULL,  -- price per room per night
    CONSTRAINT uq_rates_hotel_type_from  UNIQUE (hotel_id, room_type_id, valid_from),
    CONSTRAINT ck_rates_season CHECK (season_code IN ('LOW', 'MID', 'HIGH')),
    CONSTRAINT ck_rates_valid_dates CHECK (valid_to >= valid_from),
    CONSTRAINT ck_rates_amount_positive  CHECK (amount > 0),
    CONSTRAINT ck_rates_currency_format  CHECK (currency ~ '^[A-Z]{3}$')
);

-- One row per hotel, room type and night.
-- The team agreed on no overbooking, so availability can never go below zero.*/
CREATE TABLE IF NOT EXISTS inventory (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_id INT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_type_id  INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    inventory_date DATE NOT NULL,
    total_rooms INTEGER NOT NULL,
    available_rooms  INTEGER NOT NULL,
    CONSTRAINT uq_inventory_hotel_type_date UNIQUE (hotel_id, room_type_id, inventory_date),
    CONSTRAINT ck_inventory_total_nonneg  CHECK (total_rooms >= 0),
    CONSTRAINT ck_inventory_available_max CHECK (available_rooms <= total_rooms),
    CONSTRAINT ck_inventory_available_min CHECK (available_rooms >= 0)
);

/*
Reservations made through our own booking engine.
They are business history, so nothing deletes them by cascade:
every parent uses RESTRICT and the delete fails instead.
first_night_amount is frozen at booking time; the cancellation
penalty (RN-07) is computed from it.*/

CREATE TABLE IF NOT EXISTS reservations (
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(30)   NOT NULL,
    hotel_id  INT NOT NULL REFERENCES hotels(id) ON DELETE RESTRICT,
    room_type_id INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    channel_id INT NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    guest_id INT NOT NULL REFERENCES guests(id) ON DELETE RESTRICT,
    check_in DATE NOT NULL,
    check_out DATE NOT NULL,
    rooms_count INTEGER NOT NULL DEFAULT 1,
    adults INTEGER NOT NULL,
    children INTEGER NOT NULL DEFAULT 0,
    infants INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(12) NOT NULL DEFAULT 'confirmed',
    is_refundable BOOLEAN NOT NULL DEFAULT TRUE,
    currency CHAR(3) NOT NULL,
    first_night_amount  NUMERIC(12,2) NOT NULL,
    total_amount  NUMERIC(12,2) NOT NULL,
    created_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT uq_reservations_code UNIQUE (code),
    CONSTRAINT ck_reservations_dates CHECK (check_out > check_in),
    CONSTRAINT ck_reservations_rooms_count  CHECK (rooms_count >= 1),
    CONSTRAINT ck_reservations_guests CHECK (adults >= 1 AND children >= 0 AND infants >= 0),
    CONSTRAINT ck_reservations_status CHECK (status IN ('confirmed', 'cancelled', 'no_show')),
    CONSTRAINT ck_reservations_amounts CHECK (first_night_amount >= 0 AND total_amount >= first_night_amount),
    CONSTRAINT ck_reservations_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_reservations_cancelled_at CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);
 
/* No booking for a night that already passed: the check-in date can't be
earlier than the day the booking was made, in the hotel's own time zone
(a booking at 11 pm in Bogota is still "today" there, even if it is tomorrow in UTC). 
It compares against created_at, not against today's date, so past stays loaded by the seed stay valid. 
New bookings get created_at = NOW(), which makes this "no check-in before today".
A CHECK can't read the hotels table, hence the trigger.*/

CREATE OR REPLACE FUNCTION reservations_no_past_check_in() RETURNS trigger AS $$
DECLARE
    booked_on DATE;
BEGIN
    SELECT (NEW.created_at AT TIME ZONE h.timezone)::date INTO booked_on
    FROM hotels h WHERE h.id = NEW.hotel_id;
    IF NEW.check_in < booked_on THEN
        RAISE EXCEPTION 'check-in % is before the booking date % (hotel local time)',
            NEW.check_in, booked_on
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_reservations_no_past_check_in
    BEFORE INSERT OR UPDATE OF check_in, created_at, hotel_id ON reservations
    FOR EACH ROW EXECUTE FUNCTION reservations_no_past_check_in();

/*
Deliberately broken bookings (statement 5.3, US-26).
The checks above would reject them, so they live here instead.
The F3 extraction reads both tables; the quality layer has to
catch these. defect_type says which defect was injected. */

CREATE TABLE IF NOT EXISTS reservation_anomalies (
    id            INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          VARCHAR(30)   NOT NULL,
    hotel_id      INT           NOT NULL REFERENCES hotels(id)     ON DELETE RESTRICT,
    room_type_id  INT           NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    channel_id    INT           NOT NULL REFERENCES channels(id)   ON DELETE RESTRICT,
    guest_id      INT           NOT NULL REFERENCES guests(id)     ON DELETE RESTRICT,
    check_in      DATE          NOT NULL,
    check_out     DATE          NOT NULL,
    adults        INTEGER       NOT NULL,
    children      INTEGER       NOT NULL,
    infants       INTEGER       NOT NULL,
    currency      CHAR(3)       NOT NULL,
    total_amount  NUMERIC(12,2) NOT NULL,
    defect_type   VARCHAR(30)   NOT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_reservation_anomalies_code UNIQUE (code),
    CONSTRAINT ck_reservation_anomalies_type CHECK (defect_type IN ('zero_guests', 'checkout_before_checkin'))
);

COMMIT;
