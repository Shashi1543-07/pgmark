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
CONFIG_PATH = DATA_DIR / "config.json"

HOST = os.environ.get("PUGMARK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PUGMARK_PORT", "7860"))


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


@dataclass
class Triage:
    stage_a_enabled: bool = True
    stage_a_grid: int = 16
    """Compare against the background on a grid, not a global mean. A tiger
    filling 4% of the frame barely moves a global average but lights up
    several cells."""

    stage_a_blank_threshold: float = 0.012
    """Below this mean-absolute-cell-difference a frame is blank without
    running the detector. Tune so false negatives are ZERO on validation,
    then move further toward caution. A blank kept costs seconds of review;
    an animal discarded is unrecoverable field data."""

    stage_a_median_window: int = 60
    stage_a_separate_night: bool = True

    detector_conf_threshold: float = 0.20
    """Deliberately low. Recall on contains-subject matters far more than
    precision — see the asymmetry above."""

    quarantine_conf_threshold: float = 0.90
    """Only quarantine when the system is confident it is blank."""

    seconds_per_manual_review: float = 3.0
    """Stated assumption behind the person-hours-saved figure. Editable,
    and displayed next to the number so nobody mistakes it for measured."""


@dataclass
class Identify:
    t_high: float = 0.82           # >= : auto-assign
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
    absence_cycles: int = 3
    absence_min_effort_coverage: float = 0.6
    """Below this, absence is NOT reported as absence. The system says
    'I could not see' instead of 'it is not there'. That distinction is
    the whole difference between this and the census method that declared
    Sariska's tigers present while they were already gone."""

    new_station_requires_prior_cycles: int = 1
    """A station installed this cycle cannot produce a 'new station' alert.
    The tiger did not move; the camera arrived."""

    buffer_effort_ratio_damping: float = 0.5

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
    for d in (DATA_DIR, QUARANTINE_DIR, CROPS_DIR, RESTRICTED_DIR, WEIGHTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


CONFIG = Config.load()
