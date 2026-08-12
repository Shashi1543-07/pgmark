"""Live route tests.

These boot the real application and drive the real HTTP surface against the
real database. They exist because a route that is defined but never wired,
or a query that is written but never correct, passes every static check and
fails in front of a jury.

Rule this file enforces: assert the EFFECT, not the existence.

    python -m tests.live.test_routes
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient     # noqa: E402

from edge.app import app                       # noqa: E402
from edge.db import repo                       # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def main() -> int:
    c = TestClient(app)

    # ── meta ────────────────────────────────────────────────────────────
    h = c.get("/api/health").json()
    check("health responds", h["ok"])
    check("schema migrated", h["schema_version"] >= 1, f"v{h['schema_version']}")

    reserves = c.get("/api/reserves").json()
    check("reserve seeded", len(reserves) == 1)
    rid = reserves[0]["reserve_id"]
    check("reserve carries a UTM zone", reserves[0]["utm_epsg"] == 32644,
          "area in km² is wrong without projecting first")

    # ── runs ────────────────────────────────────────────────────────────
    runs = c.get(f"/api/runs?reserve_id={rid}").json()
    check("three cycles present", len(runs) == 3)
    run_id = runs[0]["run_id"]
    run = c.get(f"/api/runs/{run_id}").json()
    counts = run["counts"]
    check("frames counted", counts["total"] > 2000, f"{counts['total']}")
    check("blank ratio is realistic",
          counts["quarantined"] > counts["subject"] * 5,
          f"{counts['quarantined']} blank vs {counts['subject']} with a subject")
    check("timestamp fallbacks exercised",
          len(run["timestamp_sources"]) >= 3,
          str([r["src"] for r in run["timestamp_sources"]]))
    check("ingest flags surfaced", len(run["flags"]) >= 2)

    # ── triage and the undo ─────────────────────────────────────────────
    t = c.get(f"/api/runs/{run_id}/triage").json()
    before = t["summary"]["quarantined"]
    check("person-hours computed", t["summary"]["person_hours_saved"] > 0,
          f"{t['summary']['person_hours_saved']} h")
    check("assumption is disclosed",
          "seconds_per_review_assumed" in t["summary"])
    check("stage A carries real load", (t["counts"]["stage_a"] or 0) > 0,
          f"{t['counts']['stage_a']} removed before the detector ran")

    r = c.post(f"/api/runs/{run_id}/quarantine/restore",
               json={"actor": "tester"}).json()
    check("restore returns frames", r["restored"] == before, f"{r['restored']}")
    after = c.get(f"/api/runs/{run_id}/triage").json()["summary"]
    check("restore is real, not cosmetic", after["quarantined"] == 0)
    r2 = c.post(f"/api/runs/{run_id}/quarantine/restore",
                json={"actor": "tester"}).json()
    check("restore is idempotent", r2["restored"] == 0)

    # ── individuals ─────────────────────────────────────────────────────
    inds = c.get(f"/api/individuals?reserve_id={rid}").json()
    check("catalogue populated", len(inds) == 13, f"{len(inds)}")
    check("provisional individuals exist",
          any(i["provisional"] for i in inds),
          "auto-enrolled tigers must still be reviewed")
    ind = c.get(f"/api/individuals/{inds[0]['ind_id']}").json()
    check("captures joined through to stations",
          bool(ind["captures"]) and "station_name" in ind["captures"][0])

    # ── privacy: role gating is a server control, not a UI suggestion ───
    precise = c.get(f"/api/individuals/{inds[0]['ind_id']}?role=director").json()
    coarse = c.get(f"/api/individuals/{inds[0]['ind_id']}?role=analyst").json()
    p_lat = precise["captures"][0]["lat"]
    a_lat = coarse["captures"][0]["lat"]
    check("analyst coordinates are generalised", p_lat != a_lat,
          f"{p_lat} vs {a_lat}")
    audit = c.get("/api/audit?limit=50&q=location.read").json()
    check("location reads are audited", len(audit) >= 2,
          "who looked at a tiger's locations is the question that matters")

    # ── review ──────────────────────────────────────────────────────────
    rv = c.get("/api/review").json()
    check("review queue populated", rv["open"] > 0, f"{rv['open']} open")
    item = rv["items"][0]
    check("queue is prioritised by impact",
          rv["items"][0]["priority"] >= rv["items"][-1]["priority"])
    d = c.post(f"/api/review/{item['queue_id']}/decide",
               json={"ind_id": item["candidates"][1]["ind_id"],
                     "actor": "tester"}).json()
    check("decision reduces the queue", d["remaining"] == rv["open"] - 1)

    conn = repo.connect()
    sup = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE superseded_by IS NOT NULL"
    ).fetchone()["c"]
    check("corrections supersede rather than overwrite", sup >= 1,
          "the record of who thought what must survive being corrected")

    # ── occupancy ───────────────────────────────────────────────────────
    occ = c.get(f"/api/runs/{run_id}/occupancy").json()
    check("occupancy computed", len(occ) > 0)
    check("insufficient captures reported, not faked",
          any(o["insufficient_reason"] for o in occ),
          "a hull needs 3 stations; fewer must say so")

    # ── alerts: the core of the product ─────────────────────────────────
    raised = c.get(f"/api/runs/{run_id}/alerts?suppressed=false").json()
    held = c.get(f"/api/runs/{run_id}/alerts?suppressed=true").json()
    check("alerts raised", len(raised["items"]) == 4, f"{len(raised['items'])}")
    check("alerts suppressed", len(held["items"]) == 4, f"{len(held['items'])}")

    kinds = {a["type"] for a in raised["items"]}
    check("buffer-ward movement is raised", "buffer_ward" in kinds)
    check("buffer-ward is the top severity",
          any(a["type"] == "buffer_ward" and a["severity"] == "act"
              for a in raised["items"]),
          "it is the alert that precedes conflict")

    held_by_type = {a["type"]: a for a in held["items"]}
    check("absence suppressed when cameras died",
          held_by_type["absence"]["effort_coverage"] < 0.6,
          f"coverage {held_by_type['absence']['effort_coverage']}")
    check("suppression explains itself",
          all(a["suppress_reason"] for a in held["items"]))
    check("new-station suppressed for a camera installed this cycle",
          "installed this cycle" in held_by_type["new_station"]["suppress_reason"])

    real_absence = next(a for a in raised["items"] if a["type"] == "absence")
    check("absence IS raised when effort was good",
          real_absence["effort_coverage"] >= 0.6,
          f"coverage {real_absence['effort_coverage']}")

    low_id = next(a for a in held["items"] if a["ind_id"].startswith("PENCH-P"))
    check("alert confidence never exceeds the identification beneath it",
          low_id["confidence"] <= 0.5, f"{low_id['confidence']}")

    # ── audit immutability ──────────────────────────────────────────────
    try:
        conn.execute("UPDATE audit_log SET actor='x' WHERE log_id=1")
        conn.commit()
        check("audit_log blocks UPDATE", False, "update succeeded")
    except Exception:
        check("audit_log blocks UPDATE", True)
    try:
        conn.execute("DELETE FROM audit_log WHERE log_id=1")
        conn.commit()
        check("audit_log blocks DELETE", False, "delete succeeded")
    except Exception:
        check("audit_log blocks DELETE", True)

    # ── sync reports honestly ───────────────────────────────────────────
    s = c.get("/api/sync/status").json()
    check("sync reports honestly rather than pretending",
          s["enabled"] is False and bool(s["reason"]))

    # ── UI is served, and reaches for nothing off-machine ───────────────
    page = c.get("/").text
    check("page served", "PUG" in page)
    js = (Path(__file__).resolve().parents[2] / "edge/ui/app.js").read_text()
    css = (Path(__file__).resolve().parents[2] / "edge/ui/app.css").read_text()
    for name, text in (("page", page), ("script", js), ("stylesheet", css)):
        offenders = [t for t in ("http://", "https://", "cdn.", "googleapis",
                                 "unpkg", "jsdelivr", "tile.openstreetmap")
                     if t in text]
        check(f"{name} fetches nothing off this machine", not offenders,
              str(offenders))

    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
