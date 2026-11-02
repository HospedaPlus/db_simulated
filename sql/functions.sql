/* Business functions every writer shares.

FastAPI (direct bookings), the F2 Kafka consumer, the F4 agencies DAG and the
F3 simulator all book through operation.reservar(), so the overbooking rule
lives in exactly one place. They take catalog codes (HP-CO-001, DOUBLE,
expedia, G-0000001), never internal ids.

    reservar()                    book, or record a rejection (RN-03, RN-04)
    modificar()                   change dates/room type/rooms: old nights out, new nights in, or nothing
    liberar()                     give the nights back (cancelled, no_show, relocated, duplicate)
    penalidad()                   what a cancellation would cost now (RN-07), changes nothing
    cancelar()                    cancel: store the penalty and free the nights
    refresh_overbooking_limits()  daily RN-03 limits; 0 on nights with a 'high' event

CREATE OR REPLACE: running `python -m seed init` again updates them in place. */

BEGIN;

SET LOCAL search_path TO operation;

-- COP is quoted in round thousands; MXN, BRL and EUR keep cents (same as the seed).
CREATE OR REPLACE FUNCTION round_money(amount NUMERIC, currency TEXT) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN currency = 'COP' THEN round(amount, -3) ELSE round(amount, 2) END
$$;

CREATE OR REPLACE FUNCTION lead_bucket(days INTEGER) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN days <= 7 THEN '0-7' WHEN days <= 30 THEN '8-30'
                WHEN days <= 90 THEN '31-90' ELSE '91+' END
$$;


/* reservar(): the only way a booking takes rooms.

SELECT ... FOR UPDATE locks the requested nights, always in date order (no
deadlocks). With 50 buyers for the last room, the other 49 wait, then see
the room already sold and get 'rejected'. It never raises for "no room":
it returns result = 'rejected' and keeps a row in reservation_rejections,
so the rejection survives even if the caller rolls back nothing.

Same (channel, external_code) twice = the same booking: the second call
returns 'already_exists' and changes nothing (Kafka redeliveries, re-run DAGs).

p_nightly_rate: the rate the channel sold at (F2 events carry it). Leave it
NULL to price from the rates table (corporate gets CORPORATE_DISCOUNT, RN-02). */
CREATE OR REPLACE FUNCTION reservar(
    p_channel       TEXT,
    p_external_code TEXT,
    p_hotel         TEXT,
    p_room_type     TEXT,
    p_guest_ref     TEXT,
    p_check_in      DATE,
    p_check_out     DATE,
    p_rooms         INTEGER DEFAULT 1,
    p_adults        INTEGER DEFAULT 1,
    p_refundable    BOOLEAN DEFAULT TRUE,
    p_children      INTEGER DEFAULT 0,
    p_infants       INTEGER DEFAULT 0,
    p_nightly_rate  NUMERIC DEFAULT NULL,
    p_occurred_at   TIMESTAMPTZ DEFAULT NOW()
) RETURNS TABLE (result TEXT, reservation_code TEXT, reservation_id INTEGER, nights_without_room DATE[])
LANGUAGE plpgsql
SET search_path = operation, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    v_channel  channels%ROWTYPE;
    v_hotel    hotels%ROWTYPE;
    v_type     room_types%ROWTYPE;
    v_guest_id INTEGER;
    v_nights   INTEGER := p_check_out - p_check_in;
    v_found    INTEGER;
    v_full     DATE[];
    v_existing reservations%ROWTYPE;
    v_rates    NUMERIC[];
    v_code     TEXT;
    v_first    NUMERIC;
    v_total    NUMERIC;
    v_new_id   INTEGER;
BEGIN
    -- 1) Translate catalog codes; unknown codes are a caller bug, so they raise.
    SELECT * INTO v_channel FROM channels WHERE code = p_channel;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown channel %', p_channel USING ERRCODE = 'foreign_key_violation'; END IF;
    SELECT * INTO v_hotel FROM hotels WHERE code = p_hotel;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown hotel %', p_hotel USING ERRCODE = 'foreign_key_violation'; END IF;
    SELECT * INTO v_type FROM room_types WHERE code = p_room_type;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown room type %', p_room_type USING ERRCODE = 'foreign_key_violation'; END IF;
    SELECT id INTO v_guest_id FROM guests WHERE guest_ref = p_guest_ref;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown guest %', p_guest_ref USING ERRCODE = 'foreign_key_violation'; END IF;
    IF v_nights < 1 THEN
        RAISE EXCEPTION 'check-out % must be after check-in %', p_check_out, p_check_in USING ERRCODE = 'check_violation';
    END IF;
    IF p_rooms < 1 OR p_adults + p_children > v_type.max_guests * p_rooms THEN
        RAISE EXCEPTION '% guests do not fit in % % room(s)', p_adults + p_children, p_rooms, p_room_type
            USING ERRCODE = 'check_violation';
    END IF;

    -- 2) Lock the requested nights, in date order.
    SELECT count(*), coalesce(array_agg(n.inventory_date ORDER BY n.inventory_date)
                              FILTER (WHERE n.available_rooms < p_rooms), '{}')
      INTO v_found, v_full
      FROM (SELECT inventory_date, available_rooms
              FROM inventory
             WHERE hotel_id = v_hotel.id AND room_type_id = v_type.id
               AND inventory_date >= p_check_in AND inventory_date < p_check_out
             ORDER BY inventory_date
               FOR UPDATE) n;

    -- 3) Already booked? (checked after the lock, so two copies of the same
    --    request can't both get through)
    SELECT * INTO v_existing FROM reservations
     WHERE channel_id = v_channel.id AND external_code = p_external_code;
    IF FOUND THEN
        RETURN QUERY SELECT 'already_exists'::TEXT, v_existing.code::TEXT, v_existing.id, '{}'::DATE[];
        RETURN;
    END IF;

    -- 4) No room (or nights we don't sell yet): record it (RN-04) and say no.
    IF v_found <> v_nights OR cardinality(v_full) > 0 THEN
        INSERT INTO reservation_rejections (source, channel_id, external_code, hotel_id, room_type_id,
                                            check_in, check_out, rooms_count, reason, nights_without_room)
        VALUES (v_channel.source, v_channel.id, p_external_code, v_hotel.id, v_type.id,
                p_check_in, p_check_out, p_rooms,
                CASE WHEN v_found <> v_nights THEN 'no_inventory' ELSE 'no_room' END, v_full);
        RETURN QUERY SELECT 'rejected'::TEXT, NULL::TEXT, NULL::INTEGER, v_full;
        RETURN;
    END IF;

    -- 5) Price it.
    IF p_nightly_rate IS NOT NULL THEN
        v_first := round_money(p_nightly_rate * p_rooms, v_hotel.currency);
        v_total := round_money(p_nightly_rate * v_nights * p_rooms, v_hotel.currency);
    ELSE
        SELECT array_agg(round_money(r.amount * CASE WHEN v_channel.channel_type = 'corporate'
                                                     THEN 0.90 ELSE 1 END, v_hotel.currency)
                         ORDER BY d.night)
          INTO v_rates
          FROM generate_series(p_check_in, p_check_out - 1, interval '1 day') AS d(night)
          JOIN rates r ON r.hotel_id = v_hotel.id AND r.room_type_id = v_type.id
                      AND d.night::date BETWEEN r.valid_from AND r.valid_to;
        IF coalesce(cardinality(v_rates), 0) <> v_nights THEN
            RAISE EXCEPTION 'no rate for some night of % % %..%', p_hotel, p_room_type, p_check_in, p_check_out;
        END IF;
        v_first := round_money(v_rates[1] * p_rooms, v_hotel.currency);
        v_total := round_money((SELECT sum(x) FROM unnest(v_rates) x) * p_rooms, v_hotel.currency);
    END IF;

    -- 6) Sell: take the rooms and store the booking.
    UPDATE inventory SET sold_rooms = sold_rooms + p_rooms
     WHERE hotel_id = v_hotel.id AND room_type_id = v_type.id
       AND inventory_date >= p_check_in AND inventory_date < p_check_out;

    v_code := CASE WHEN v_channel.source = 'F3' THEN p_external_code
                   ELSE v_channel.source || '-' || p_external_code END;
    INSERT INTO reservations (code, source, external_code, hotel_id, room_type_id, channel_id, guest_id,
                              check_in, check_out, rooms_count, adults, children, infants, status,
                              is_refundable, currency, first_night_amount, total_amount, last_event_at)
    VALUES (v_code, v_channel.source, p_external_code, v_hotel.id, v_type.id, v_channel.id, v_guest_id,
            p_check_in, p_check_out, p_rooms, p_adults, p_children, p_infants, 'confirmed',
            p_refundable, v_hotel.currency, v_first, v_total, p_occurred_at)
    RETURNING id INTO v_new_id;

    RETURN QUERY SELECT 'confirmed'::TEXT, v_code, v_new_id, '{}'::DATE[];
END;
$$;


/* modificar(): a channel changes a confirmed booking (new dates, room type,
rooms or guests). Frees the old nights and takes the new ones in one step:
if the new nights have no room, nothing changes, the rejection is stored
(RN-04) and result = 'rejected'. NULL arguments keep the current value.

    result = 'modified'        done
             'rejected'        no room for the new stay; booking unchanged
             'not_found'       the channel never created it here (an event that
                               arrived before its 'created': keep it pending)
             'not_modifiable'  cancelled, no-show, relocated or duplicate
             'stale'           p_occurred_at is older than the last applied event */
CREATE OR REPLACE FUNCTION modificar(
    p_channel       TEXT,
    p_external_code TEXT,
    p_room_type     TEXT DEFAULT NULL,
    p_check_in      DATE DEFAULT NULL,
    p_check_out     DATE DEFAULT NULL,
    p_rooms         INTEGER DEFAULT NULL,
    p_adults        INTEGER DEFAULT NULL,
    p_children      INTEGER DEFAULT NULL,
    p_infants       INTEGER DEFAULT NULL,
    p_nightly_rate  NUMERIC DEFAULT NULL,
    p_occurred_at   TIMESTAMPTZ DEFAULT NOW()
) RETURNS TABLE (result TEXT, reservation_code TEXT, nights_without_room DATE[])
LANGUAGE plpgsql
SET search_path = operation, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    v_channel channels%ROWTYPE;
    r         reservations%ROWTYPE;
    v_type    room_types%ROWTYPE;
    v_cur     TEXT;
    v_in      DATE;
    v_out     DATE;
    v_rooms   INTEGER;
    v_adults  INTEGER;
    v_children INTEGER;
    v_infants INTEGER;
    v_nights  INTEGER;
    v_found   INTEGER;
    v_full    DATE[];
    v_rates   NUMERIC[];
    v_first   NUMERIC;
    v_total   NUMERIC;
BEGIN
    SELECT * INTO v_channel FROM channels WHERE code = p_channel;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown channel %', p_channel USING ERRCODE = 'foreign_key_violation'; END IF;

    SELECT * INTO r FROM reservations
     WHERE channel_id = v_channel.id AND external_code = p_external_code FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::TEXT, NULL::TEXT, '{}'::DATE[]; RETURN;
    END IF;
    IF r.status <> 'confirmed' THEN
        RETURN QUERY SELECT 'not_modifiable'::TEXT, r.code::TEXT, '{}'::DATE[]; RETURN;
    END IF;
    IF r.last_event_at IS NOT NULL AND p_occurred_at <= r.last_event_at THEN
        RETURN QUERY SELECT 'stale'::TEXT, r.code::TEXT, '{}'::DATE[]; RETURN;
    END IF;

    IF p_room_type IS NULL THEN
        SELECT * INTO v_type FROM room_types WHERE id = r.room_type_id;
    ELSE
        SELECT * INTO v_type FROM room_types WHERE code = p_room_type;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown room type %', p_room_type USING ERRCODE = 'foreign_key_violation'; END IF;
    END IF;
    SELECT currency INTO v_cur FROM hotels WHERE id = r.hotel_id;
    v_in := coalesce(p_check_in, r.check_in);
    v_out := coalesce(p_check_out, r.check_out);
    v_rooms := coalesce(p_rooms, r.rooms_count);
    v_adults := coalesce(p_adults, r.adults);
    v_children := coalesce(p_children, r.children);
    v_infants := coalesce(p_infants, r.infants);
    v_nights := v_out - v_in;
    IF v_nights < 1 THEN
        RAISE EXCEPTION 'check-out % must be after check-in %', v_out, v_in USING ERRCODE = 'check_violation';
    END IF;
    IF v_rooms < 1 OR v_adults + v_children > v_type.max_guests * v_rooms THEN
        RAISE EXCEPTION '% guests do not fit in % % room(s)', v_adults + v_children, v_rooms, v_type.code
            USING ERRCODE = 'check_violation';
    END IF;

    -- Lock old and new nights together, always in the same order (no deadlocks).
    PERFORM 1 FROM inventory
      WHERE hotel_id = r.hotel_id
        AND ((room_type_id = r.room_type_id AND inventory_date >= r.check_in AND inventory_date < r.check_out)
          OR (room_type_id = v_type.id AND inventory_date >= v_in AND inventory_date < v_out))
      ORDER BY room_type_id, inventory_date FOR UPDATE;

    -- Free the old nights, then check the new ones.
    UPDATE inventory SET sold_rooms = sold_rooms - r.rooms_count
     WHERE hotel_id = r.hotel_id AND room_type_id = r.room_type_id
       AND inventory_date >= r.check_in AND inventory_date < r.check_out;

    SELECT count(*), coalesce(array_agg(inventory_date ORDER BY inventory_date)
                              FILTER (WHERE available_rooms < v_rooms), '{}')
      INTO v_found, v_full
      FROM inventory
     WHERE hotel_id = r.hotel_id AND room_type_id = v_type.id
       AND inventory_date >= v_in AND inventory_date < v_out;

    IF v_found <> v_nights OR cardinality(v_full) > 0 THEN
        -- No room: put the old nights back, keep the booking as it was.
        UPDATE inventory SET sold_rooms = sold_rooms + r.rooms_count
         WHERE hotel_id = r.hotel_id AND room_type_id = r.room_type_id
           AND inventory_date >= r.check_in AND inventory_date < r.check_out;
        INSERT INTO reservation_rejections (source, channel_id, external_code, hotel_id, room_type_id,
                                            check_in, check_out, rooms_count, reason, nights_without_room)
        VALUES (v_channel.source, v_channel.id, p_external_code, r.hotel_id, v_type.id, v_in, v_out, v_rooms,
                CASE WHEN v_found <> v_nights THEN 'no_inventory' ELSE 'no_room' END, v_full);
        RETURN QUERY SELECT 'rejected'::TEXT, r.code::TEXT, v_full; RETURN;
    END IF;

    UPDATE inventory SET sold_rooms = sold_rooms + v_rooms
     WHERE hotel_id = r.hotel_id AND room_type_id = v_type.id
       AND inventory_date >= v_in AND inventory_date < v_out;

    -- Re-price: the channel's rate if given, otherwise the rates table.
    IF p_nightly_rate IS NOT NULL THEN
        v_first := round_money(p_nightly_rate * v_rooms, v_cur);
        v_total := round_money(p_nightly_rate * v_nights * v_rooms, v_cur);
    ELSE
        SELECT array_agg(round_money(rt.amount * CASE WHEN v_channel.channel_type = 'corporate'
                                                      THEN 0.90 ELSE 1 END, v_cur) ORDER BY d.night)
          INTO v_rates
          FROM generate_series(v_in, v_out - 1, interval '1 day') AS d(night)
          JOIN rates rt ON rt.hotel_id = r.hotel_id AND rt.room_type_id = v_type.id
                       AND d.night::date BETWEEN rt.valid_from AND rt.valid_to;
        IF coalesce(cardinality(v_rates), 0) <> v_nights THEN
            RAISE EXCEPTION 'no rate for some night of % %..%', v_type.code, v_in, v_out;
        END IF;
        v_first := round_money(v_rates[1] * v_rooms, v_cur);
        v_total := round_money((SELECT sum(x) FROM unnest(v_rates) x) * v_rooms, v_cur);
    END IF;

    UPDATE reservations
       SET room_type_id = v_type.id, check_in = v_in, check_out = v_out, rooms_count = v_rooms,
           adults = v_adults, children = v_children, infants = v_infants,
           first_night_amount = v_first, total_amount = v_total, last_event_at = p_occurred_at
     WHERE id = r.id;

    RETURN QUERY SELECT 'modified'::TEXT, r.code::TEXT, '{}'::DATE[];
END;
$$;


/* liberar(): a confirmed booking stops holding rooms (cancelled, no_show,
relocated or duplicate). Returns false, and does nothing, if the booking
wasn't confirmed: calling it twice is harmless. */
CREATE OR REPLACE FUNCTION liberar(p_code TEXT, p_new_status TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql
SET search_path = operation, pg_temp
AS $$
DECLARE
    r reservations%ROWTYPE;
BEGIN
    IF p_new_status NOT IN ('cancelled', 'no_show', 'relocated', 'duplicate') THEN
        RAISE EXCEPTION 'liberar(): unsupported status %', p_new_status USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO r FROM reservations WHERE code = p_code AND status = 'confirmed' FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    -- lock the nights in date order, like reservar(), so the two never deadlock
    PERFORM 1 FROM inventory
      WHERE hotel_id = r.hotel_id AND room_type_id = r.room_type_id
        AND inventory_date >= r.check_in AND inventory_date < r.check_out
      ORDER BY inventory_date FOR UPDATE;
    UPDATE inventory SET sold_rooms = sold_rooms - r.rooms_count
     WHERE hotel_id = r.hotel_id AND room_type_id = r.room_type_id
       AND inventory_date >= r.check_in AND inventory_date < r.check_out;
    UPDATE reservations
       SET status = p_new_status,
           cancelled_at = CASE WHEN p_new_status = 'cancelled' THEN NOW() END
     WHERE id = r.id;
    RETURN TRUE;
END;
$$;


/* penalidad(): what cancelling at p_at would cost (RN-07). Changes nothing,
so the guest API can show it before the guest confirms.
Check-in is 15:00 in the hotel's own time zone; that's what makes 72 h and
24 h right for a hotel in Lisbon and one in Bogota alike. */
CREATE OR REPLACE FUNCTION penalidad(p_code TEXT, p_at TIMESTAMPTZ DEFAULT NOW())
RETURNS TABLE (reservation_code TEXT, hours_before NUMERIC, penalty_pct NUMERIC,
               penalty_amount NUMERIC, currency TEXT, rule TEXT)
LANGUAGE plpgsql STABLE
SET search_path = operation, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    r       reservations%ROWTYPE;
    v_type  TEXT;
    v_tz    TEXT;
    v_hours NUMERIC;
    p       cancellation_policies%ROWTYPE;
BEGIN
    SELECT * INTO r FROM reservations WHERE code = p_code;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown reservation %', p_code USING ERRCODE = 'no_data_found';
    END IF;
    SELECT c.channel_type, h.timezone INTO v_type, v_tz
      FROM channels c, hotels h WHERE c.id = r.channel_id AND h.id = r.hotel_id;
    v_hours := round((extract(epoch FROM ((r.check_in + TIME '15:00') AT TIME ZONE v_tz) - p_at) / 3600)::NUMERIC, 1);

    SELECT * INTO p FROM cancellation_policies cp
     WHERE cp.channel_type = v_type AND cp.is_refundable = r.is_refundable
       AND cp.hours_from <= greatest(v_hours, 0)
       AND (cp.hours_to IS NULL OR greatest(v_hours, 0) < cp.hours_to)
     ORDER BY cp.hours_from DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no cancellation policy for % (refundable=%)', v_type, r.is_refundable;
    END IF;

    RETURN QUERY SELECT
        r.code::TEXT, v_hours, p.penalty_pct,
        round_money(CASE WHEN p.penalty_base = 'first_night' THEN r.first_night_amount ELSE r.total_amount END
                    * p.penalty_pct / 100, r.currency),
        r.currency::TEXT,
        format('%s %s-%s h', CASE WHEN r.is_refundable THEN 'refundable' ELSE 'non_refundable' END,
               p.hours_from, coalesce(p.hours_to::TEXT, 'inf'));
END;
$$;


/* cancelar(): cancel a confirmed booking at p_at, store its penalty and give
the nights back. result = 'not_cancellable' if it wasn't confirmed. */
CREATE OR REPLACE FUNCTION cancelar(p_code TEXT, p_at TIMESTAMPTZ DEFAULT NOW())
RETURNS TABLE (result TEXT, reservation_code TEXT, hours_before NUMERIC, penalty_pct NUMERIC,
               penalty_amount NUMERIC, currency TEXT, rule TEXT)
LANGUAGE plpgsql
SET search_path = operation, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    q RECORD;
BEGIN
    SELECT * INTO q FROM penalidad(p_code, p_at);
    IF NOT liberar(p_code, 'cancelled') THEN
        RETURN QUERY SELECT 'not_cancellable'::TEXT, p_code, NULL::NUMERIC, NULL::NUMERIC,
                            NULL::NUMERIC, NULL::TEXT, NULL::TEXT;
        RETURN;
    END IF;
    UPDATE reservations SET penalty_amount = q.penalty_amount, cancelled_at = p_at WHERE code = p_code;
    RETURN QUERY SELECT 'cancelled'::TEXT, q.reservation_code, q.hours_before, q.penalty_pct,
                        q.penalty_amount, q.currency, q.rule;
END;
$$;


/* refresh_overbooking_limits(): RN-03, run once a day by the
limites_sobreventa_diario DAG after Spark writes cancellation_rate_by_lead.
    limit = min(5% of the rooms, floor(cancel rate x rooms sold) - 1), never below 0
    nights with a 'high' local event (F6) -> 0
    hotels with no cancellation rate yet -> 0 (fails on the safe side)
If a limit drops below what's already sold, nothing is cancelled: the limit
stays at what's sold and no more rooms are sold. Returns the rows changed. */
CREATE OR REPLACE FUNCTION refresh_overbooking_limits(p_today DATE DEFAULT CURRENT_DATE) RETURNS INTEGER
LANGUAGE plpgsql
SET search_path = operation, pg_temp
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    WITH calc AS (
        SELECT i.id,
               CASE WHEN EXISTS (SELECT 1 FROM local_events e
                                  WHERE e.hotel_code = h.code AND e.category = 'high'
                                    AND i.inventory_date BETWEEN e.start_date AND e.end_date)
                    THEN 0
                    ELSE greatest(0, least(floor(0.05 * i.total_rooms)::INTEGER,
                                           coalesce(floor(t.cancel_rate * i.sold_rooms)::INTEGER - 1, 0)))
               END AS new_limit
          FROM inventory i
          JOIN hotels h ON h.id = i.hotel_id
          LEFT JOIN cancellation_rate_by_lead t
                 ON t.hotel_code = h.code AND t.lead_bucket = lead_bucket(i.inventory_date - p_today)
         WHERE i.inventory_date >= p_today
    )
    UPDATE inventory i
       SET overbooking_limit = least(i.total_rooms, greatest(c.new_limit, i.sold_rooms - i.total_rooms))
      FROM calc c
     WHERE c.id = i.id
       AND i.overbooking_limit <> least(i.total_rooms, greatest(c.new_limit, i.sold_rooms - i.total_rooms));
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;

COMMIT;
