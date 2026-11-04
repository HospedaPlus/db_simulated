/* F3 operational database, in the `operation` schema.

F2's event ledger lives next door in the `streaming` schema of the same
database. Run as the owner role (hospedaplus_owner on Neon): its default
privileges give app_writer read/write and etl_reader read-only access to
every table created here. */

BEGIN;

CREATE SCHEMA IF NOT EXISTS operation;
-- LOCAL: only for this transaction, so it also works behind a connection pooler.
SET LOCAL search_path TO operation;


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
    timezone VARCHAR(64)  NOT NULL,          -- prices go out before 06:00 local time; penalties use it
    currency CHAR(3) NOT NULL,          -- the currency this hotel sells in
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hotels_code  UNIQUE (code),
    CONSTRAINT ck_hotels_category CHECK (category IN ('urban', 'resort')),
    CONSTRAINT ck_hotels_currency CHECK (currency ~ '^[A-Z]{3}$')
);

-- Commission per channel comes from RN-02. source = which simulated source
-- sells through the channel (F2 OTAs, F3 own engine, F4 agencies).
CREATE TABLE IF NOT EXISTS channels (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(30)  NOT NULL,
    name  VARCHAR(80)  NOT NULL,
    channel_type    VARCHAR(10)  NOT NULL,
    commission_pct  NUMERIC(5,2) NOT NULL DEFAULT 0,
    source  VARCHAR(3) NOT NULL,
    CONSTRAINT uq_channels_code UNIQUE (code),
    CONSTRAINT ck_channels_type CHECK (channel_type IN ('direct', 'ota', 'agency', 'corporate')),
    CONSTRAINT ck_channels_commission_pct CHECK (commission_pct BETWEEN 0 AND 100),
    CONSTRAINT ck_channels_source CHECK (source IN ('F2', 'F3', 'F4'))
);

-- Personal data lives only here (RN-11: pseudonymize before analytics).
-- guest_ref is the shared pseudonymous code F2 events carry (G-0000001 ...).
CREATE TABLE IF NOT EXISTS guests (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guest_ref VARCHAR(12) NOT NULL,
    full_name  VARCHAR(120) NOT NULL,
    email  VARCHAR(180),
    country  CHAR(2),
    loyalty_tier  VARCHAR(10)  NOT NULL DEFAULT 'none',   -- gold guests are never relocated (RN-09)
    CONSTRAINT uq_guests_guest_ref UNIQUE (guest_ref),
    CONSTRAINT ck_guests_guest_ref CHECK (guest_ref ~ '^G-[0-9]{7}$'),
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
-- Pickup and event multipliers (RN-05/RN-06) are applied later by the pricing service.
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

/* One row per hotel, room type and night (the solution's "inventario_diario").
sold_rooms only changes through reservar() / liberar().
overbooking_limit (RN-03) is set once a day by refresh_overbooking_limits();
it starts at 0, which means no overbooking at all.
available_rooms is computed by the database, never written. */
CREATE TABLE IF NOT EXISTS inventory (
    id  INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_id INT NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_type_id  INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    inventory_date DATE NOT NULL,
    total_rooms INTEGER NOT NULL,                 -- physical rooms
    sold_rooms INTEGER NOT NULL DEFAULT 0,
    overbooking_limit INTEGER NOT NULL DEFAULT 0,
    available_rooms INTEGER GENERATED ALWAYS AS (total_rooms + overbooking_limit - sold_rooms) STORED,
    CONSTRAINT uq_inventory_hotel_type_date UNIQUE (hotel_id, room_type_id, inventory_date),
    CONSTRAINT ck_inventory_total_nonneg  CHECK (total_rooms >= 0),
    CONSTRAINT ck_inventory_sold_nonneg CHECK (sold_rooms >= 0),
    CONSTRAINT ck_inventory_limit_range CHECK (overbooking_limit BETWEEN 0 AND total_rooms),
    -- safety net: never sell past capacity + the allowed overbooking
    CONSTRAINT ck_inventory_within_limit CHECK (sold_rooms <= total_rooms + overbooking_limit)
);

/*
Reservations from every channel. F3's own engine (direct, corporate) and,
through reservar(), F2 (OTAs) and F4 (agencies) too.
They are business history, so nothing deletes them by cascade:
every parent uses RESTRICT and the delete fails instead.
first_night_amount is frozen at booking time; the cancellation
penalty (RN-07) is computed from it.
Only 'confirmed' reservations hold rooms in the inventory.*/

CREATE TABLE IF NOT EXISTS reservations (
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code VARCHAR(100)   NOT NULL,             -- our locator (F3-0000001, F2-<ota id>, ...)
    source VARCHAR(3) NOT NULL,               -- F2, F3 or F4
    external_code VARCHAR(80) NOT NULL,       -- the id the channel uses
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
    penalty_amount NUMERIC(12,2),             -- set by cancelar() (RN-07)
    duplicate_of INT REFERENCES reservations(id) ON DELETE RESTRICT,  -- RN-08
    created_at TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    cancelled_at TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ,                -- newest channel event applied; older ones are ignored
    CONSTRAINT uq_reservations_code UNIQUE (code),
    CONSTRAINT uq_reservations_channel_external UNIQUE (channel_id, external_code),
    CONSTRAINT ck_reservations_source CHECK (source IN ('F2', 'F3', 'F4')),
    CONSTRAINT ck_reservations_dates CHECK (check_out > check_in),
    CONSTRAINT ck_reservations_rooms_count  CHECK (rooms_count >= 1),
    CONSTRAINT ck_reservations_guests CHECK (adults >= 1 AND children >= 0 AND infants >= 0),
    CONSTRAINT ck_reservations_status CHECK (status IN ('confirmed', 'cancelled', 'no_show',
                                                        'relocated', 'duplicate')),
    CONSTRAINT ck_reservations_amounts CHECK (first_night_amount >= 0 AND total_amount >= first_night_amount),
    CONSTRAINT ck_reservations_penalty CHECK (penalty_amount IS NULL OR penalty_amount BETWEEN 0 AND total_amount),
    CONSTRAINT ck_reservations_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_reservations_cancelled_at CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);

/* No booking for a night that already passed: the check-in date can't be
earlier than the day the booking was made, in the hotel's own time zone
(a booking at 11 pm in Bogota is still "today" there, even if it is tomorrow in UTC).
It compares against created_at, not against today's date, so past stays loaded by the seed stay valid.
New bookings get created_at = NOW(), which makes this "no check-in before today".
A CHECK can't read the hotels table, hence the trigger.*/

CREATE OR REPLACE FUNCTION reservations_no_past_check_in() RETURNS trigger
SET search_path = operation, pg_temp AS $$
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

/* updated_at: when the row last changed in this database, whatever the change
(new booking, cancellation, no-show, relocation, late OTA event). The lake
copies reservations incrementally by this column, so a change is never missed
even when the event itself happened earlier (last_event_at). Existing rows
take the time the column is added. */
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE OR REPLACE FUNCTION reservations_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_reservations_updated_at
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION reservations_touch_updated_at();

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

/* RN-07 cancellation policy, per channel type and kind of rate.
Changing a policy = changing rows, not code. The seed loads RN-07 for the
four channel types. hours_* count back from check-in (15:00 hotel time):
hours_from included, hours_to excluded, NULL = no upper limit. */
CREATE TABLE IF NOT EXISTS cancellation_policies (
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_type VARCHAR(10) NOT NULL,
    is_refundable BOOLEAN NOT NULL,
    hours_from INTEGER NOT NULL,
    hours_to INTEGER,
    penalty_pct NUMERIC(5,2) NOT NULL,
    penalty_base VARCHAR(12) NOT NULL,       -- first_night or total
    CONSTRAINT uq_cancellation_policies UNIQUE (channel_type, is_refundable, hours_from),
    CONSTRAINT ck_cancellation_policies_type CHECK (channel_type IN ('direct', 'ota', 'agency', 'corporate')),
    CONSTRAINT ck_cancellation_policies_hours CHECK (hours_from >= 0 AND (hours_to IS NULL OR hours_to > hours_from)),
    CONSTRAINT ck_cancellation_policies_pct CHECK (penalty_pct BETWEEN 0 AND 100),
    CONSTRAINT ck_cancellation_policies_base CHECK (penalty_base IN ('first_night', 'total'))
);

/* RN-04: every booking reservar() refuses, with the nights that had no room.
Written by the system, never by the seed. */
CREATE TABLE IF NOT EXISTS reservation_rejections (
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source VARCHAR(3) NOT NULL,
    channel_id INT NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    external_code VARCHAR(80) NOT NULL,
    hotel_id INT NOT NULL REFERENCES hotels(id) ON DELETE RESTRICT,
    room_type_id INT NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    check_in DATE NOT NULL,
    check_out DATE NOT NULL,
    rooms_count INTEGER NOT NULL,
    reason VARCHAR(20) NOT NULL,             -- no_room or no_inventory
    nights_without_room DATE[] NOT NULL DEFAULT '{}',
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_reservation_rejections_reason CHECK (reason IN ('no_room', 'no_inventory'))
);

/* RN-09: guests moved to another hotel because of overbooking.
Written by the daily relocation DAG (Airflow), then liberar(code, 'relocated'). */
CREATE TABLE IF NOT EXISTS relocations (
    reservation_id INT NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    relocation_date DATE NOT NULL,
    reason VARCHAR(30) NOT NULL DEFAULT 'overbooking',
    estimated_cost NUMERIC(12,2),
    currency CHAR(3),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reservation_id, relocation_date)
);

/* Inputs of the overbooking limit (RN-03). Keyed by hotel code, not id, so
re-seeding never wipes them: F6 and the Spark job own these rows. */
CREATE TABLE IF NOT EXISTS local_events (            -- F6 local events CSV
    id INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_code VARCHAR(12) NOT NULL,
    name VARCHAR(120) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,                   -- inclusive
    category VARCHAR(6) NOT NULL,             -- high = no overbooking at all
    CONSTRAINT ck_local_events_dates CHECK (end_date >= start_date),
    CONSTRAINT ck_local_events_category CHECK (category IN ('low', 'medium', 'high'))
);

CREATE TABLE IF NOT EXISTS cancellation_rate_by_lead (  -- written daily by Spark (F1 + bookings)
    hotel_code VARCHAR(12) NOT NULL,
    lead_bucket VARCHAR(5) NOT NULL,
    cancel_rate NUMERIC(5,4) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (hotel_code, lead_bucket),
    CONSTRAINT ck_cancellation_rate_bucket CHECK (lead_bucket IN ('0-7', '8-30', '31-90', '91+')),
    CONSTRAINT ck_cancellation_rate_range CHECK (cancel_rate BETWEEN 0 AND 1)
);

COMMIT;
