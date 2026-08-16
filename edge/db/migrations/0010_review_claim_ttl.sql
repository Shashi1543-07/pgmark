-- 0010 — Review claim leak fix.
--
-- claim_review_item() moved an item to state='claimed' and only
-- release_review_item() could move it back. Nothing called release when a
-- browser tab was closed or reloaded, and there was no expiry, so every
-- refresh of the review screen permanently burned the item on screen out
-- of state='open'. review_open_page() filters on state='open', so the item
-- silently vanished from the queue with no decision recorded, no audit
-- trail of a decision, and no way to get it back from the interface.
--
-- Two columns make the claim recoverable: who holds it, and since when.
-- A claim older than the TTL is not a lock any more, it is debris.

ALTER TABLE review_queue ADD COLUMN claimed_by TEXT;
ALTER TABLE review_queue ADD COLUMN claimed_at TEXT;

-- Anything already stranded in 'claimed' by the old code has no owner and
-- no timestamp, so it can never expire on its own. Release it now.
UPDATE review_queue SET state='open' WHERE state='claimed';

CREATE INDEX IF NOT EXISTS ix_review_claimed ON review_queue(state, claimed_at);
