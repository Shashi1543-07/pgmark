-- PUGMARK · migration 0003 · the entity model
--
-- WHY THIS EXISTS
-- The ATRW paper (Li et al., ACM MM 2020) establishes that the unit of tiger
-- re-identification is not the individual but the ENTITY: one side of one
-- tiger. Left and right flank patterns are different and unrelated, and it is
-- rare to capture both sides of the same animal in the wild, so the two sides
-- are treated as separate entities throughout.
--
-- ATRW itself has 182 entities from 92 tigers — close to both sides each,
-- because it was shot in zoos. The paper's reason for splitting sides applies
-- to the WILD, where capturing both flanks of one animal is rare. Camera traps
-- are the wild case, so a catalogue keyed on the individual implies a
-- completeness our data will not have.
--
-- Migration 0001 had `flank_crops.side`, which was necessary but not
-- sufficient: it recorded the side of a crop without making the side part of
-- the catalogue's identity. This migration fixes that.

CREATE TABLE entities (
  entity_id   TEXT PRIMARY KEY,          -- 'PENCH-014-L'
  ind_id      TEXT NOT NULL REFERENCES individuals(ind_id),
  side        TEXT NOT NULL CHECK(side IN ('L','R')),
  crop_count  INTEGER NOT NULL DEFAULT 0,
  first_seen  TEXT,
  last_seen   TEXT,
  UNIQUE(ind_id, side)
);
CREATE INDEX ix_entities_ind ON entities(ind_id);

-- Matching happens entity to entity. An L crop is scored only against L
-- entities. Enforcing that in the query rather than in a comment is the whole
-- reason this column exists.
ALTER TABLE assignments ADD COLUMN entity_id TEXT REFERENCES entities(entity_id);
CREATE INDEX ix_assign_entity ON assignments(entity_id)
  WHERE superseded_by IS NULL;

-- Backfill: every existing assignment already knows its crop, and every crop
-- already knows its side.
INSERT OR IGNORE INTO entities (entity_id, ind_id, side, crop_count)
SELECT a.ind_id || '-' || c.side, a.ind_id, c.side, COUNT(*)
  FROM assignments a
  JOIN flank_crops c ON c.crop_id = a.crop_id
 WHERE a.superseded_by IS NULL AND c.side IN ('L','R')
 GROUP BY a.ind_id, c.side;

UPDATE assignments
   SET entity_id = (
     SELECT a2.ind_id || '-' || c.side
       FROM assignments a2
       JOIN flank_crops c ON c.crop_id = a2.crop_id
      WHERE a2.assign_id = assignments.assign_id
        AND c.side IN ('L','R'))
 WHERE entity_id IS NULL;

-- A view for the case the paper says is the common one, not the edge case:
-- a tiger for which only one flank has ever been photographed. A crop of the
-- other side cannot be matched to it AND cannot be used as evidence of a new
-- tiger. The correct output is "unresolvable", and the UI has to be able to
-- ask which individuals are in that state.
CREATE VIEW single_flank_individuals AS
SELECT i.ind_id,
       i.reserve_id,
       MIN(e.side) AS seen_side,
       COUNT(e.entity_id) AS sides_seen
  FROM individuals i
  JOIN entities e ON e.ind_id = i.ind_id
 GROUP BY i.ind_id, i.reserve_id
HAVING COUNT(e.entity_id) = 1;
