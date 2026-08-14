"""The eight alert scenarios, driven entirely from generated data.

    python -m tests.scenarios.test_alert_scenarios

CLAUDE.md and AUDIT_AND_REVISED_PLAN.md are explicit that these eight
rows ARE the specification for edge/pipeline/alerts.py:

    | Scenario                                                    | Expected     |
    |--------------------------------------------------------------|-------------|
    | first capture at a buffer station near a village              | buffer_ward, act |
    | centroid moved past the core limit                            | centroid_shift fires |
    | absent, cameras in its range worked all cycle                 | absence fires |
    | first use of a station active since before this cycle         | new_station fires |
    | absent, but its cameras died partway through the cycle        | suppressed: low effort coverage |
    | first capture at a camera installed this cycle                | suppressed: camera arrived, not the tiger |
    | centroid moved but only 2 events                               | suppressed: below the 5-event minimum |
    | buffer capture at identification confidence 0.44               | suppressed: alert can't exceed the ID beneath it |

tools/seed_demo.py proves the same eight scenarios through the full
pipeline (real files, ingest, triage, matching) end to end -- proof the
*product* works. This file proves generate_for_run() alone reproduces
the same eight outcomes from nothing but database rows built directly
by tests/scenarios/synth_history.py, independent of everything upstream.
One reserve, eight individuals, one shared 3-cycle timeline; each
individual isolated to its own stations so no scenario's data can leak
into another's outcome.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge.db import repo                                    # noqa: E402
from edge.pipeline import alerts                             # noqa: E402
from tests.scenarios.synth_history import History, day        # noqa: E402

RESERVE = "rsv_alert_scenarios"


def _alert(alerts_list, ind_id, typ):
    return next((a for a in alerts_list if a["ind_id"] == ind_id and a["type"] == typ), None)


def build() -> tuple[History, str]:
    h = History(RESERVE)

    # A shared "home range" cluster most individuals were already using
    # in cycles 0 and 1, so a first visit elsewhere in cycle 2 is a real
    # departure, not just an artifact of having no history at all.
    h.station("CORE_A1", "core", 21.600, 79.300, 12.0)
    h.station("CORE_A2", "core", 21.601, 79.301, 12.0)

    # 1. buffer_ward fires.
    h.station("BUF_A", "buffer", 21.605, 79.302, 1.0)
    # 2. centroid_shift fires (4.1 km, past the ~2.36 km core limit).
    h.station("CORE_FAR", "core", 21.636937, 79.300, 10.0)
    # 3. absence fires -- home range stays fully surveyed.
    h.station("CORE_ABSENT1", "core", 21.550, 79.250, 14.0)
    h.station("CORE_ABSENT2", "core", 21.551, 79.251, 14.0)
    # 4. new_station fires -- station predates this cycle.
    h.station("ESTABLISHED_NEW", "core", 21.610, 79.310, 13.0)
    # 5. absence suppressed -- these die 4 days into the current cycle.
    h.station("DEAD1", "core", 21.500, 79.200, 14.0, active_to_day=2 * 30 + 4)
    h.station("DEAD2", "core", 21.501, 79.201, 14.0, active_to_day=2 * 30 + 4)
    # 6. new_station suppressed -- installed exactly at the current cycle.
    h.station("NEWCAM", "core", 21.620, 79.320, 13.0, active_from_day=2 * 30)
    # 7. centroid_shift suppressed -- same 4.1 km jump, too few events.
    h.station("CORE_FAR2", "core", 21.636937, 79.310, 10.0)
    # 8. buffer_ward suppressed -- low identity confidence.
    h.station("BUF_LOWCONF", "buffer", 21.606, 79.303, 0.8)

    run0 = h.run(0, "cycle-0")
    run1 = h.run(1, "cycle-1")
    run2 = h.run(2, "cycle-2")

    # 1. IND_BUFFER: core history, first buffer capture this cycle.
    h.occupancy(run0, "IND_BUFFER", ["CORE_A1", "CORE_A2"], 6, 21.6005, 79.3005)
    h.occupancy(run1, "IND_BUFFER", ["CORE_A1", "CORE_A2"], 6, 21.6005, 79.3005)
    h.occupancy(run2, "IND_BUFFER", ["BUF_A"], 3, 21.605, 79.302)

    # 2. IND_SHIFT: moves 4.1 km, plenty of events both cycles.
    h.occupancy(run1, "IND_SHIFT", ["CORE_A1", "CORE_A2"], 6, 21.600, 79.300)
    h.occupancy(run2, "IND_SHIFT", ["CORE_FAR"], 6, 21.636937, 79.300)

    # 3. IND_ABSENT_OK: present two cycles running, then nothing --
    # cameras in its range stayed on the whole time.
    h.occupancy(run0, "IND_ABSENT_OK", ["CORE_ABSENT1", "CORE_ABSENT2"], 4, 21.550, 79.250)
    h.occupancy(run1, "IND_ABSENT_OK", ["CORE_ABSENT1", "CORE_ABSENT2"], 4, 21.550, 79.250)
    # (no run2 row: absent)

    # 4. IND_NEWSTATION: detected elsewhere while ESTABLISHED_NEW was
    # already active, then shows up there for the first time.
    h.occupancy(run0, "IND_NEWSTATION", ["CORE_A1", "CORE_A2"], 5, 21.6005, 79.3005)
    h.occupancy(run1, "IND_NEWSTATION", ["CORE_A1", "CORE_A2"], 5, 21.6005, 79.3005)
    h.occupancy(run2, "IND_NEWSTATION", ["ESTABLISHED_NEW"], 3, 21.610, 79.310)

    # 5. IND_ABSENT_SUPPRESSED: also absent this cycle, but its own
    # cameras died 4 days in -- effort coverage collapses.
    h.occupancy(run0, "IND_ABSENT_SUPPRESSED", ["DEAD1", "DEAD2"], 4, 21.500, 79.200)
    h.occupancy(run1, "IND_ABSENT_SUPPRESSED", ["DEAD1", "DEAD2"], 4, 21.500, 79.200)
    # (no run2 row: absent)

    # 6. IND_NEWCAM_SUPPRESSED: detected elsewhere, then a capture at a
    # camera that was only installed this cycle -- the tiger didn't move.
    h.occupancy(run0, "IND_NEWCAM_SUPPRESSED", ["CORE_A1", "CORE_A2"], 5, 21.6005, 79.3005)
    h.occupancy(run1, "IND_NEWCAM_SUPPRESSED", ["CORE_A1", "CORE_A2"], 5, 21.6005, 79.3005)
    h.occupancy(run2, "IND_NEWCAM_SUPPRESSED", ["NEWCAM"], 2, 21.620, 79.320)

    # 7. IND_SHIFT_WEAK: the same 4.1 km jump as IND_SHIFT, but only 2
    # events -- below the 5-event minimum, noise not movement.
    h.occupancy(run1, "IND_SHIFT_WEAK", ["CORE_A1", "CORE_A2"], 5, 21.600, 79.300)
    h.occupancy(run2, "IND_SHIFT_WEAK", ["CORE_FAR2"], 2, 21.636937, 79.310)

    # 8. IND_LOWCONF: no prior history at all (a brand-new individual),
    # first capture in the buffer -- but the identification behind it is
    # weak, so the alert cannot inherit more confidence than that.
    h.occupancy(run2, "IND_LOWCONF", ["BUF_LOWCONF"], 2, 21.606, 79.303)
    h.identified_capture(run2, "IND_LOWCONF", "BUF_LOWCONF", 0.44, captured_at=day(2 * 30 + 1))

    return h, run2


def run_scenarios() -> tuple[list[str], list[str]]:
    """Builds the synthetic reserve, runs generate_for_run() once, checks
    all eight outcomes against the spec table, tears the reserve down
    again, and returns (passed, failed) name lists -- no printing, so the
    live suite (tests/live/test_routes.py) can fold this straight into
    its own pass/fail accounting instead of shelling out."""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}{' — ' + detail if detail else ''}")

    repo.delete_reserve_cascade(RESERVE)   # in case a prior run was interrupted
    h, run2 = build()
    try:
        out = alerts.generate_for_run(run2)

        a = _alert(out, "IND_BUFFER", "buffer_ward")
        check("1. buffer_ward fires at severity 'act' for a first buffer capture",
              a is not None and not a["suppressed"] and a["severity"] == "act", str(a))

        a = _alert(out, "IND_SHIFT", "centroid_shift")
        check("2. centroid_shift fires when the centroid moves past the core limit",
              a is not None and not a["suppressed"], str(a))

        a = _alert(out, "IND_ABSENT_OK", "absence")
        check("3. absence fires when cameras in the individual's range worked all cycle",
              a is not None and not a["suppressed"], str(a))

        a = _alert(out, "IND_NEWSTATION", "new_station")
        check("4. new_station fires for a genuinely new station, active before this cycle",
              a is not None and not a["suppressed"], str(a))

        a = _alert(out, "IND_ABSENT_SUPPRESSED", "absence")
        check("5. absence is suppressed when the individual's own cameras died mid-cycle",
              a is not None and a["suppressed"]
              and "coverage" in (a["suppress_reason"] or "").lower(), str(a))

        a = _alert(out, "IND_NEWCAM_SUPPRESSED", "new_station")
        check("6. new_station is suppressed for a camera installed this cycle",
              a is not None and a["suppressed"]
              and "installed this cycle" in (a["suppress_reason"] or ""), str(a))

        a = _alert(out, "IND_SHIFT_WEAK", "centroid_shift")
        check("7. centroid_shift is suppressed below the 5-event minimum",
              a is not None and a["suppressed"]
              and "minimum" in (a["suppress_reason"] or "").lower(), str(a))

        a = _alert(out, "IND_LOWCONF", "buffer_ward")
        check("8. buffer_ward is suppressed when identity confidence is below review threshold",
              a is not None and a["suppressed"]
              and "identity confidence" in (a["suppress_reason"] or "").lower(), str(a))
    finally:
        repo.delete_reserve_cascade(RESERVE)

    return passed, failed


def main() -> int:
    passed, failed = run_scenarios()
    print("scenario  outcome")
    for name in passed:
        print(f"  ok   {name}")
    for name in failed:
        print(f"  FAIL {name}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

