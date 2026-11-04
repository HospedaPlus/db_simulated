BEGIN;

SET LOCAL search_path TO operation;

CREATE INDEX IF NOT EXISTS idx_rooms_room_type_id          ON rooms(room_type_id);
CREATE INDEX IF NOT EXISTS idx_rates_room_type_id          ON rates(room_type_id);
CREATE INDEX IF NOT EXISTS idx_inventory_date              ON inventory(inventory_date);
CREATE INDEX IF NOT EXISTS idx_inventory_room_type_id      ON inventory(room_type_id);

CREATE INDEX IF NOT EXISTS idx_reservations_hotel_checkin  ON reservations(hotel_id, check_in);
CREATE INDEX IF NOT EXISTS idx_reservations_room_type_id   ON reservations(room_type_id);
CREATE INDEX IF NOT EXISTS idx_reservations_channel_id     ON reservations(channel_id);
CREATE INDEX IF NOT EXISTS idx_reservations_guest_id       ON reservations(guest_id);
CREATE INDEX IF NOT EXISTS idx_reservations_status_checkin ON reservations(status, check_in);
CREATE INDEX IF NOT EXISTS idx_reservations_created_at     ON reservations(created_at);  -- incremental extraction
CREATE INDEX IF NOT EXISTS idx_reservations_updated_at     ON reservations(updated_at);  -- incremental extraction (lake)
CREATE INDEX IF NOT EXISTS idx_reservations_duplicate_of   ON reservations(duplicate_of);

CREATE INDEX IF NOT EXISTS idx_anomalies_hotel_id          ON reservation_anomalies(hotel_id);
CREATE INDEX IF NOT EXISTS idx_rejections_rejected_at      ON reservation_rejections(rejected_at);
CREATE INDEX IF NOT EXISTS idx_local_events_hotel_dates    ON local_events(hotel_code, start_date, end_date);

COMMIT;
