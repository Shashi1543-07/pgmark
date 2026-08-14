"""PUGMARK configuration.

Every threshold in the system lives here. Nothing is hardcoded in a pipeline
module. Three reasons:

  1. Every threshold is a policy decision the forest department should own,
     not a number an engineer buried in a function.
  2. The active config is written into runs.config, so any result can be
     explained years later.
  3. The UI renders this file, so an officer can see what the machine was
     told before judging what it decided.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SCHEMA_VERSION = 1
APP_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PUGMARK_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "pugmark.db"
QUARANTINE_DIR = DATA_DIR / "quarantine"
CROPS_DIR = DATA_DIR / "crops"
RESTRICTED_DIR = DATA_DIR / "restricted"
WEIGHTS_DIR = DATA_DIR / "weights"
UPLOADS_DIR = DATA_DIR / "uploads"
CONFIG_PATH = DATA_DIR / "config.json"

HOST = os.environ.get("PUGMARK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PUGMARK_PORT", "7860"))

SYNC_SECRET = os.environ.get("PUGMARK_SYNC_SECRET", "")
"""Shared HMAC key for signing sync bundles between trusted nodes. Not
part of the Config dataclass on purpose: that object is serialised into
runs.config and rendered on the Ops screen, and a secret has no business
in either place. Empty by default -- sync refuses to build or apply a
bundle rather than sign with a blank key."""


@dataclass
class Ingest:
    burst_window_s: int = 10
    """Frames at one station within this window are ONE event. Camera traps
    fire 2-5 shot bursts; without grouping, one tiger walking past becomes
    three 'captures' and every occupancy statistic is inflated."""

    min_plausible_year: int = 2005
    max_future_days: int = 1
    folder_match_max_edit_distance: int = 2
    timestamp_band_frac: float = 0.08
    """Bottom fraction of the frame that holds the camera's burned-in
    date/time strip. Read by OCR when EXIF is missing or reset."""

    estimated_seconds_per_image: float = 0.05
    """Stated assumption behind preflight's processing-time estimate --
    scan and hash only, since triage's detector isn't built yet. Editable,
    and shown next to the estimate so nobody mistakes it for measured."""


@dataclass
class Triage:
    stage_a_enabled: bool = True
    stage_a_grid: int = 16
    """Compare against the background on a grid, not a global mean. A tiger
    filling 4% of the frame barely moves a global average but lights up
    several cells."""

    stage_a_blank_threshold: float = 0.03
    """The score is the MAXIMUM per-cell difference against the background
    (edge/pipeline/triage.py::cell_score), not a mean or a percentile: a
    16x16 grid has 256 cells, so a subject in a single cell is 1/256 of
    the frame and would be outvoted by the other 255 under anything less
    than the max. Below this score, a frame is blank without running the
    detector.

    This is the ONLY gate -- there used to be a second, derived
    confidence threshold here too, and the two disagreed by 10x without
    either number on screen ever admitting it (AUDIT_AND_REVISED_PLAN.md
    P0-2/P0-3). One threshold, and the value shown is the value in force.

    Tuned against tests/unit/test_triage_scoring.py's worst case (a
    single lit cell at the lowest tested contrast, which scores 0.047)
    with headroom to spare, since a synthetic worst case is not the same
    as having actually measured this against labelled data -- that
    measurement is P2-7 in the audit, not done yet. Tune so false
    negatives are ZERO on real validation, then move further toward
    caution. A blank kept costs seconds of review; an animal discarded is
    unrecoverable field data."""

    stage_a_median_window: int = 60
    """Cap on how many frames contribute to a station/night background.
    Below this, every frame in the group is used; above it, an evenly
    spaced sample across the whole (sorted) group is taken, so the cost
    stays bounded on a very active station without the sample depending on
    which frames happened to be processed first."""

    stage_a_min_frames_for_background: int = 3
    """Below this many frames in a station/night group, there is no
    meaningful background to compare against -- a median of one or two
    frames is mostly just the frame itself. Every frame in a group this
    small stays pending rather than being scored against a background
    that begs the question (CLAUDE.md rule 8: refusing to answer is a
    valid output). AUDIT_AND_REVISED_PLAN.md P2-6."""

    stage_a_separate_night: bool = True

    detector_conf_threshold: float = 0.20
    """Deliberately low. Recall on contains-subject matters far more than
    precision — see the asymmetry above."""

    batch_size: int = 200
    """Frames per checkpointed batch. A crash loses at most one batch."""

    torch_threads: int = 0
    """0 = leave PyTorch's default (every core). On a 4-core range-office
    laptop that makes the machine unusable for anything else for the hours
    a 50K run takes. 2 or 3 leaves the officer a working computer."""

    seconds_per_manual_review: float = 3.0
    """Stated assumption behind the person-hours-saved figure. Editable,
    and displayed next to the number so nobody mistakes it for measured."""


@dataclass
class Identify:
    t_high: float = 0.95           # >= : auto-assign
    """Deliberately conservative, not the original 0.82 guess. Measured
    against tools/eval_identify.py's open-set genuine/impostor score
    distributions (ATRW, held out by identity): at 0.82, 14.7% of novel
    tigers -- ones the catalogue has never seen -- were wrongly
    auto-accepted as a match to an existing entity (docs/RESULTS.md,
    "Open-set separation and threshold calibration"). That corrupts the
    catalogue in a way superseded_by can correct after the fact
    (CLAUDE.md rule 5) but should not be relying on routinely. The
    calibration there suggested ~0.946 as the point where impostor
    auto-accept drops to ~1%; 0.95 rounds that up rather than down.
    This trades away auto-accept convenience for catalogue safety on
    purpose, and needs re-measuring against Pench's own data once any
    exists -- see docs/RESULTS.md's own caution that this number comes
    from Amur zoo tigers, not a validated Pench operating point."""

    t_low: float = 0.55            # >= : human review;  < : enrol provisional
    ensemble_embed_weight: float = 0.6
    top_k_candidates: int = 5
    min_quality: float = 0.35
    """Below this the crop is not matched at all. Refusing is a correct
    answer; a confident wrong match corrupts the catalogue permanently."""

    min_crop_pixels: int = 4096
    enforce_side_separation: bool = True
    """Left and right flank patterns are DIFFERENT, not mirrored. Never
    score an L crop against an R catalogue. Turning this off is a bug."""

    require_side_classifier: bool = True
    """Bulk Stage 3 will not auto-assign or auto-enrol while this is True.

    edge/pipeline/keypoints.py labels every prediction "right_shoulder"/
    "right_hip" regardless of which flank is actually showing -- confirmed
    empirically in docs/RESULTS.md's wild evaluation, where every held-out
    image came back side='R' and none 'L'. On a real deployment roughly
    half of captures show the left flank and would be scored against the
    RIGHT-side catalogue. One photograph at a time that is a bad match a
    human can catch; 50,000 at a time it is a corrupted catalogue.

    While True, crops and embeddings are still computed and stored -- the
    work is not thrown away -- but every decision goes to the review queue
    with the reason stated. Set False only once a side classifier exists.
    CLAUDE.md rule 8: refusing to answer is a valid output."""

    species_gate: str = "review"
    """strict | review | off.

    MegaDetector's `animal` class is not `tiger`. v0.1.1 sent every animal
    detection into the flank pipeline, so a leopard, sambar or wild dog
    could be embedded, scored against the tiger catalogue, and below t_low
    enrolled as a brand-new tiger. On a real reserve most animal detections
    are not tigers, so that is the common case, not the rare one.

      strict -- only detections confirmed as target_species proceed. With
                no species classifier installed, that is none, and the run
                says so rather than pretending.
      review -- crops and embeddings are computed, decisions go to a human.
                The honest default while no classifier exists.
      off    -- v0.1.1's behaviour. Recorded in the audit log; not advised."""

    target_species: str = "tiger"

    rect_body_depth_ratio: float = 0.6
    """edge/pipeline/identify.py::rectify_flank(). A profile photograph
    never shows the far shoulder+hip -- checked against 905 side-resolved
    ATRW training crops, the opposite side is unlabelled in every single
    one, so rectification cannot be a real 4-point quadrilateral warp.
    Instead the near-side shoulder-hip line sets the body axis, and the
    perpendicular (dorsal-ventral) extent is this fraction of that
    line's length -- a stated anatomical approximation, not a measured
    point. Tune against real crops once available; 0.6 is a starting
    guess, not a fitted value."""

    rect_margin_ratio: float = 0.15
    """How far the rectified crop extends past the shoulder and the hip
    along the body axis, as a fraction of the shoulder-hip distance --
    enough to catch a little neck and rump stripe pattern beyond the two
    anchor points, not so much that it pulls in background."""


@dataclass
class Occupancy:
    min_stations_for_hull: int = 3
    """Below this an MCP is undefined. Report insufficient_reason rather
    than emitting a degenerate polygon."""

    home_range_method: str = "mcp"


@dataclass
class Alerts:
    core_threshold_mode: str = "area"
    """The problem statement says '15-20 sq km' for core and '5 kms' for
    buffer — an area and a distance. Rather than silently guessing, both
    interpretations are implemented and the mode is stated on the slide.
      'area'     -> core_shift_area_km2 converted to an equivalent radius
      'distance' -> core_shift_distance_km used directly
    """
    core_shift_area_km2: float = 17.5      # midpoint of the 15-20 range
    core_shift_distance_km: float = 4.7
    buffer_shift_km: float = 5.0

    min_events_for_centroid: int = 5
    absence_cycles: int = 2
    """'Regular across the previous K cycles' (blueprint default 3) needs
    K+1 cycles of history to ever fire. The demo reserve runs 3 cycles
    total, so K=2 is the largest value that can be demonstrated end to
    end; a reserve with a longer run history should raise this."""
    absence_min_effort_coverage: float = 0.6
    """Below this, absence is NOT reported as absence. The system says
    'I could not see' instead of 'it is not there'. That distinction is
    the whole difference between this and the census method that declared
    Sariska's tigers present while they were already gone."""

    new_station_requires_prior_cycles: int = 1
    """A station installed this cycle cannot produce a 'new station' alert.
    The tiger did not move; the camera arrived."""

    buffer_effort_ratio_damping: float = 0.5
    buffer_effort_spike_ratio: float = 1.2
    """Above this ratio of current-cycle to historical buffer-zone camera-
    days, a buffer capture is weaker evidence -- more buffer cameras were
    simply watching, not necessarily more tigers using the buffer -- so
    rule strength is damped rather than taken at face value."""

    default_cycle_days: int = 90
    """Fallback cycle length used only when a reserve has too little run
    history to infer one from actual gaps between runs (see effort.py)."""

    @property
    def core_shift_km(self) -> float:
        """Core centroid-shift threshold in km under the active mode."""
        if self.core_threshold_mode == "area":
            return math.sqrt(self.core_shift_area_km2 / math.pi)
        return self.core_shift_distance_km


@dataclass
class Privacy:
    blur_person_boxes: bool = True
    person_image_retention_days: int = 90
    wildlife_image_retention_days: int = 3650
    generalise_coords_for_roles: tuple = ("analyst",)
    grid_cell_km: float = 2.0
    """Roles above reserve level see grid cells, not points. National
    analysis needs distribution, not the tree the tigress sleeps under."""


@dataclass
class Config:
    ingest: Ingest = field(default_factory=Ingest)
    triage: Triage = field(default_factory=Triage)
    identify: Identify = field(default_factory=Identify)
    occupancy: Occupancy = field(default_factory=Occupancy)
    alerts: Alerts = field(default_factory=Alerts)
    privacy: Privacy = field(default_factory=Privacy)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["alerts"]["core_shift_km_effective"] = round(self.alerts.core_shift_km, 3)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        if not path.exists():
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt config must never stop the app from starting. The
            # officer gets defaults and a flag, not a crash on launch.
            return cfg
        for f in fields(cls):
            section = raw.get(f.name)
            if not isinstance(section, dict):
                continue
            target = getattr(cfg, f.name)
            for key, value in section.items():
                if hasattr(target, key):
                    setattr(target, key, value)
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")


def ensure_dirs() -> None:
    for d in (DATA_DIR, QUARANTINE_DIR, CROPS_DIR, RESTRICTED_DIR, WEIGHTS_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)


CONFIG = Config.load()
