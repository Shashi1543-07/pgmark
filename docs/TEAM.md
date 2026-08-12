# Working as five people on this repo

The schema is frozen (`edge/db/migrations/0001_init.sql`). Everyone codes
against it, so nobody blocks on anybody for more than an hour.

## Ownership — one person per module, no shared files

| Who | Files |
|---|---|
| Lead | `edge/db/`, `edge/pipeline/orchestrator.py`, `edge/pipeline/alerts.py`, `edge/effort.py`, `tests/` |
| P2   | `edge/pipeline/ingest.py` |
| P3   | `edge/pipeline/triage.py`, `edge/models/detector.py` |
| P4   | `edge/pipeline/identify.py`, `edge/models/embedder.py` |
| P5   | `edge/ui/` |

`edge/app.py` and `edge/config.py` are touched by everyone — keep those edits
small and shout in the group before changing an existing route or threshold.

## Branches

    git checkout -b ingest      # or triage / identify / alerts / ui
    # ... work ...
    python -m tests.live.test_routes     # must be green before you push
    git push -u origin ingest

Merge to `main` only with the live suite passing. During the 24 hours, merge
often — a six-hour-old branch is a merge conflict waiting to happen.

## Definition of done for any module

1. It runs on the demo database.
2. It has at least one check in `tests/live/test_routes.py` that asserts its
   effect, not its existence.
3. `python -m tests.live.test_routes` is green.
4. Anything it deliberately does NOT do is written down.
