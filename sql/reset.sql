/*DESTRUCTIVE: drops every table of the operational database.
Development/test databases only. The seed runner must refuse to
execute this file when DATABASE_URL does not point to localhost
unless the user confirms explicitly.
*/

DROP TABLE IF EXISTS reservations, inventory, rates, rooms, guests, channels, hotels, room_types CASCADE;
 