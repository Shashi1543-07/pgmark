"""Edge-to-edge sync: build, sign, verify, apply. See blueprint §3.1, §15.

A sync bundle is a file, not a socket: signed JSON, small enough to
travel over HTTP, a USB stick, or a phone hotspot at 2 a.m., and safe to
apply twice. It carries every row this node has written to the four
syncable tables (runs, stations, images, individuals -- the ones the
frozen schema stamps with origin_node/lamport/synced_at/row_hash) since
the caller's last-seen Lamport value.

Not included: audit_log. Blueprint's prose describes bundles as "rows
plus the audit log", but the frozen schema never actually gave audit_log
the same origin_node/lamport/row_hash scaffolding the other four tables
have -- its primary key is a local AUTOINCREMENT with no cross-node
identity, so there is nothing here to merge it by. Extending it would be
a schema change, which is out of scope for this pass; scoping it out
honestly beats bolting on an ad hoc mechanism the schema doesn't support.

Signing is HMAC-SHA256 over a canonical JSON encoding of everything
except the signature field, using a secret shared out-of-band between
trusted nodes (PUGMARK_SYNC_SECRET). This is a deliberately simpler
trust model than public-key signing -- verifying a bundle requires
holding the same secret used to sign it, which is fine between nodes
that already trust each other (the same reserve, the same organisation)
and wrong for a scheme where any party should be able to verify a
bundle without being able to forge one. A multi-organisation deployment
would want asymmetric signatures instead; this build doesn't have one.

No SQL lives here (repo.py owns all of it).
"""
from __future__ import annotations

import hashlib
import hmac
import json

from edge.db import repo


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, default=str).encode()


def build_bundle(reserve_id: str, since_lamport: int, secret: str, conn=None) -> dict:
    """Everything this node has written for `reserve_id` since
    since_lamport, signed. Raises rather than signing with a blank key
    if no secret is configured -- an unsigned bundle is exactly what
    blueprint §10 says a receiving node must reject, so one should never
    be produced in the first place."""
    if not secret:
        raise ValueError("no sync secret configured -- set PUGMARK_SYNC_SECRET")

    node = repo.node_id(conn)
    rows: dict[str, list[dict]] = {}
    up_to = since_lamport
    for table, pk_col in repo.SYNCABLE_TABLES.items():
        table_rows = [r for r in repo.rows_since(table, since_lamport, conn)
                      if r.get("reserve_id") == reserve_id]
        rows[table] = table_rows
        repo.mark_synced(table, [r[pk_col] for r in table_rows], conn)
        for r in table_rows:
            up_to = max(up_to, r["lamport"])

    body = {
        "reserve_id": reserve_id, "origin_node": node, "created_at": repo.now(),
        "since_lamport": since_lamport, "up_to_lamport": up_to, "rows": rows,
    }
    body["signature"] = hmac.new(secret.encode(), _canonical(body), hashlib.sha256).hexdigest()
    return body


def verify_bundle(bundle: dict, secret: str) -> bool:
    """Constant-time comparison -- a bundle's signature is exactly the
    kind of thing a timing side-channel could leak bit by bit."""
    if not secret or "signature" not in bundle:
        return False
    body = {k: v for k, v in bundle.items() if k != "signature"}
    expected = hmac.new(secret.encode(), _canonical(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, bundle["signature"])


def apply_bundle(bundle: dict, secret: str, conn=None) -> dict:
    """Idempotent: every row goes through repo.apply_synced_row(), which
    is a no-op for content already seen (row_hash match) and resolves a
    genuine conflict by Lamport value rather than overwriting blind.
    Assume this gets called with the same bundle twice -- it must be
    harmless the second time, and is."""
    if not verify_bundle(bundle, secret):
        raise ValueError(
            "bundle signature does not verify -- refusing to apply an unsigned "
            "or tampered bundle")

    stats = {"inserted": 0, "unchanged": 0, "conflict_resolved": 0}
    for table, table_rows in bundle.get("rows", {}).items():
        if table not in repo.SYNCABLE_TABLES:
            continue
        for row in table_rows:
            outcome = repo.apply_synced_row(table, row, conn)
            stats[outcome] = stats.get(outcome, 0) + 1
    # This node's own clock must catch up to what it just learned, or its
    # own next edit could be stamped with a Lamport value lower than
    # something it has already seen -- see advance_lamport_watermark().
    repo.advance_lamport_watermark(bundle["up_to_lamport"], conn)
    return {**stats, "reserve_id": bundle["reserve_id"], "origin_node": bundle["origin_node"],
            "up_to_lamport": bundle["up_to_lamport"]}
