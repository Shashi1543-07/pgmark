# Pugmark

Automated camera-trap triage and individual tiger movement intelligence.
Runs offline, on CPU, on an ordinary laptop at a range office.

## Run it

    ./launcher/run.sh          # or launcher\run.bat on Windows
    # open http://127.0.0.1:7860

First launch seeds a demonstration reserve (Pench, 36 stations, 3 monitoring
cycles, ~10,000 frames) so every screen has real data before the vision
pipeline exists. Everything it contains is synthetic and labelled as such.

## Verify it

    python3 -m tests.live.test_routes

42 checks against a real server boot and a real database. These assert
effects, not existence — a route that is defined but never wired passes a
static check and fails in front of a jury.

## What is built and what is not

Built and working end to end:
  data contract (schema + migrations) · run and preflight reporting ·
  triage accounting with reversible quarantine · individual catalogue ·
  human review queue with supersede-not-overwrite corrections · occupancy
  and offline map · alert surface including suppressions · append-only
  audit with role-gated location reads · ops and drift indicators

Designed, interface defined, not yet implemented:
  the CV pipeline itself (motion prefilter, detector, flank rectification,
  stripe matching) · sync bundles and the central tier · Camtrap DP export

Say which is which. A working edge node plus a credible architecture beats
four half-built tiers.

## Layout

    edge/db/migrations/   the frozen data contract — freeze first, code against it
    edge/db/repo.py       every SQL statement in the system, and nowhere else
    edge/config.py        every threshold, user-editable, rendered in the UI
    edge/app.py           HTTP surface; role gating enforced here, not in the UI
    edge/ui/              the interface: no framework, no build step, no CDN
    tools/seed_demo.py    the synthetic reserve, including 8 alert scenarios
    tests/live/           real server, real database, asserts effects
