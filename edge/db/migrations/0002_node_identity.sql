-- PUGMARK · migration 0002 · sync identity
-- 0001_init.sql already carries origin_node/lamport/synced_at/row_hash on
-- every syncable table (runs, stations, images, individuals); what was
-- missing was somewhere for THIS node to keep its own identity and its
-- own Lamport counter, both of which must survive a restart.

CREATE TABLE node_identity (
  node_id         TEXT NOT NULL,
  lamport_counter INTEGER NOT NULL DEFAULT 0
);
