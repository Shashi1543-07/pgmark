"""Camtrap DP export. See blueprint §11: the actual argument for national
deployment is fitting between systems that already exist, not being
impressive on its own. Camtrap DP -- the Camera Trap Data Package,
developed under TDWG with GBIF -- is the community exchange format; this
module is what makes Pugmark's output "readable by the wider ecosystem
on day one" rather than a private schema nobody else can open.

Field names below were checked against the live specification
(camtrap-dp.tdwg.org/data/) rather than written from memory -- this is a
practical, honest subset (every required field, plus the optional ones
this schema can actually fill), not a claim of full spec validation.

Mapping (blueprint's own words): stations + station_activity ->
Deployments, images -> Media, detections + assignments -> Observations.

No SQL lives here -- callers pass rows already read via repo.py.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

CAMTRAP_DP_PROFILE = (
    "https://raw.githubusercontent.com/tdwg/camtrap-dp/1.0/camtrap-dp-profile.json"
)


def _deployment_lookup(activity_rows: list[dict]) -> dict[str, list[dict]]:
    by_station: dict[str, list[dict]] = {}
    for a in activity_rows:
        by_station.setdefault(a["station_id"], []).append(a)
    return by_station


def _deployment_for(station_id: str, captured_at: str | None,
                    by_station: dict[str, list[dict]]) -> str | None:
    """The activity interval covering a timestamp, at that station. Falls
    back to the station's earliest interval if nothing covers it exactly
    (a genuinely malformed case, not one this schema should silently drop
    a media row over) -- and to None only if the station has no recorded
    activity at all."""
    intervals = by_station.get(station_id)
    if not intervals:
        return None
    if captured_at:
        for a in intervals:
            if a["start_date"] <= captured_at and (not a["end_date"] or captured_at <= a["end_date"]):
                return a["activity_id"]
    return intervals[0]["activity_id"]


def deployments_table(activity_rows: list[dict], run: dict,
                      include_locations: bool = True) -> list[dict]:
    fallback_end = run.get("finished_at") or run["started_at"]
    out = []
    for a in activity_rows:
        row = {
            "deploymentID": a["activity_id"],
            "locationID": a["station_id"],
            "locationName": a["name"],
            "latitude": a["lat"] if include_locations else "",
            "longitude": a["lon"] if include_locations else "",
            "deploymentStart": a["start_date"],
            "deploymentEnd": a["end_date"] or fallback_end,
            "deploymentComments": a.get("note") or "",
        }
        out.append(row)
    return out


def media_table(image_rows: list[dict], by_station: dict[str, list[dict]]) -> list[dict]:
    out = []
    for im in image_rows:
        dep = _deployment_for(im["station_id"], im.get("captured_at"), by_station)
        if not dep:
            continue   # no recorded deployment at all: nothing to attach this media to
        out.append({
            "mediaID": im["image_id"],
            "deploymentID": dep,
            "timestamp": im.get("captured_at") or "",
            "filePath": im["orig_path"],
            "filePublic": im.get("status") != "person",
            "fileName": Path(im["orig_path"]).name,
            "fileMediatype": "image/jpeg",
        })
    return out


_LABEL_TO_OBSERVATION_TYPE = {"animal": "animal", "person": "human", "vehicle": "vehicle"}


def observations_table(detection_rows: list[dict], blank_image_rows: list[dict],
                       by_station: dict[str, list[dict]]) -> list[dict]:
    """One row per detection, plus one 'blank' row per frame the motion
    prefilter or detector called empty -- a Camtrap DP consumer reading
    only the animal rows would otherwise have no way to tell 'this media
    was never classified' apart from 'this media was classified and had
    nothing in it', which is exactly the distinction blueprint's own
    quality gates exist to preserve."""
    out = []
    for d in detection_rows:
        dep = _deployment_for(d["station_id"], d.get("captured_at"), by_station)
        if not dep:
            continue
        when = d.get("captured_at") or ""
        species = d.get("species")
        sci_name = "Panthera tigris" if species == "tiger" else (species or "")
        out.append({
            "observationID": d["det_id"],
            "deploymentID": dep,
            "mediaID": d["image_id"],
            "eventID": d.get("event_id") or "",
            "eventStart": when, "eventEnd": when,
            "observationLevel": "media",
            "observationType": _LABEL_TO_OBSERVATION_TYPE.get(d["label"], "unclassified"),
            "scientificName": sci_name,
            "count": 1,
            "individualID": d.get("ind_id") or "",
            "classificationMethod": "human" if d.get("assign_method") == "human" else "machine",
            "classificationProbability": d.get("assign_confidence") or d.get("conf") or "",
        })
    for im in blank_image_rows:
        dep = _deployment_for(im["station_id"], im.get("captured_at"), by_station)
        if not dep:
            continue
        when = im.get("captured_at") or ""
        out.append({
            "observationID": f"blank_{im['image_id']}",
            "deploymentID": dep,
            "mediaID": im["image_id"],
            "eventID": "",
            "eventStart": when, "eventEnd": when,
            "observationLevel": "media",
            "observationType": "blank",
            "scientificName": "", "count": "", "individualID": "",
            "classificationMethod": "machine", "classificationProbability": "",
        })
    return out


def datapackage_descriptor(reserve: dict, run: dict) -> dict:
    return {
        "name": f"pugmark-{reserve['reserve_id']}-{run['run_id']}".lower(),
        "profile": CAMTRAP_DP_PROFILE,
        "created": datetime.now().isoformat(timespec="seconds"),
        "title": f"{reserve['name']} camera trap data -- {run.get('cycle_label') or run['run_id']}",
        "resources": [
            {"name": "deployments", "path": "deployments.csv", "profile": "tabular-data-resource"},
            {"name": "media", "path": "media.csv", "profile": "tabular-data-resource"},
            {"name": "observations", "path": "observations.csv", "profile": "tabular-data-resource"},
        ],
        "sources": [{"title": "Pugmark edge node", "path": "https://github.com/"}],
    }


def build_package(reserve: dict, run: dict, activity_rows: list[dict],
                  image_rows: list[dict], detection_rows: list[dict],
                  include_locations: bool = True) -> dict[str, str]:
    """Returns {filename: csv/json text}, ready to be zipped as-is."""
    import csv
    import io

    by_station = _deployment_lookup(activity_rows)
    blanks = [i for i in image_rows if i["status"] in ("blank", "quarantined")]

    def to_csv(rows: list[dict]) -> str:
        if not rows:
            return ""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    return {
        "datapackage.json": json.dumps(datapackage_descriptor(reserve, run), indent=2),
        "deployments.csv": to_csv(deployments_table(activity_rows, run, include_locations)),
        "media.csv": to_csv(media_table(image_rows, by_station)),
        "observations.csv": to_csv(observations_table(detection_rows, blanks, by_station)),
    }
