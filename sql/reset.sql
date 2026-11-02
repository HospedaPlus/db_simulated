/*DESTRUCTIVE: drops every F3 table and function in the `operation` schema.
Development/test databases only. The seed runner refuses to execute this
file when DATABASE_URL does not point to localhost unless the user
confirms explicitly (--allow-remote).

The schema itself is kept on purpose: on Neon it carries the grants and
default privileges for app_writer and etl_reader. The `streaming` schema
(F2) is never touched.
*/

BEGIN;

CREATE SCHEMA IF NOT EXISTS operation;
SET LOCAL search_path TO operation;

DROP TABLE IF EXISTS relocations, reservation_rejections, reservation_anomalies, reservations,
    inventory, rates, rooms, guests, channels, hotels, room_types,
    cancellation_policies, local_events, cancellation_rate_by_lead CASCADE;

DROP FUNCTION IF EXISTS reservar, modificar, liberar, penalidad, cancelar, refresh_overbooking_limits,
    round_money, lead_bucket, reservations_no_past_check_in CASCADE;

COMMIT;
