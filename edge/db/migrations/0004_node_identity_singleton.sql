-- PUGMARK · migration 0004 · node_identity singleton
--
-- 0002_node_identity.sql created node_identity with no primary key and no
-- uniqueness constraint, so nothing stopped a second row from existing --
-- a retry, a double-run of setup, two concurrent first requests each
-- finding the table empty. Two rows means two identities, and because
-- origin_node is exactly what sync merges rows on (edge/sync/bundle.py),
-- that ambiguity would spread silently to every node this one ever syncs
-- with. AUDIT_AND_REVISED_PLAN.md P1-5.
--
-- CHECK (id = 1) makes a second row impossible at the database level,
-- not by convention -- create, copy, drop, rename, since SQLite has no
-- ADD CONSTRAINT.

CREATE TABLE node_identity_new (
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  node_id         TEXT NOT NULL CHECK (length(node_id) > 0),
  lamport_counter INTEGER NOT NULL DEFAULT 0
);
INSERT INTO node_identity_new (id, node_id, lamport_counter)
SELECT 1, node_id, lamport_counter FROM node_identity LIMIT 1;
DROP TABLE node_identity;
ALTER TABLE node_identity_new RENAME TO node_identity;
