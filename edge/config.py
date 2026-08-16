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

# These must be established before any model library is imported. They are
# deliberately assigned (rather than setdefault) so a stale shell setting
# cannot quietly re-enable a model downloader on the field laptop.
for _offline_var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "YOLO_OFFLINE"):
    os.environ[_offline_var] = "1"

SCHEMA_VERSION = 1
APP_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PUGMARK_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "pugmark.db"
QUARANTINE_DIR = DATA_DIR / "quarantine"
CROPS_DIR = DATA_DIR / "crops"
RESTRICTED_DIR = DATA_DIR / "restricted"
UPLOADS_DIR = DATA_DIR / "uploads"
# Model assets are application files, not mutable data. In particular they
# must not live below PUGMARK_DATA: an operator may relocate that directory,
# but the signed model bundle must stay with the release.
MODELS_DIR = (ROOT / "edge" / "models").resolve()
MODEL_MANIFEST_PATH = MODELS_DIR / "manifest.json"


def local_model_path(relative_path: str) -> Path:
    """Return an absolute path contained by the shipped model bundle.

    This deliberately does not check existence. A missing asset is reported
    by readiness/package verification, while import-time checks stay cheap
    and do not make the server unstartable.
    """
    candidate = (MODELS_DIR / relative_path).resolve()
    try:
        candidate.relative_to(MODELS_DIR)
    except ValueError as exc:
        raise ValueError(f"model path escapes edge/models: {relative_path!r}") from exc
    return candidate


DETECTOR_CHECKPOINT_PATH = local_model_path("megadetector/MDV6-mit-yolov9-c.ckpt")
DETECTOR_CONFIG_PATH = local_model_path("megadetector/config_v9s.yaml")
KEYPOINTS_MODEL_PATH = local_model_path("keypoints/pose_2kp.pt")
EMBEDDER_MODEL_PATH = local_model_path("identify/identify_embedder.pt")
SPECIES_MODEL_PATH = local_model_path("species/species_classifier.ts")
SIDE_MODEL_PATH = local_model_path("side/flank_side_classifier.ts")
# Training may use this optional checkpoint, but it too must be supplied as a
# local package asset. TorchVision's automatic ImageNet download is banned.
RESNET50_BACKBONE_PATH = local_model_path("backbones/resnet50-imagenet.pth")


def runtime_model_paths() -> dict[str, Path]:
    """The complete set of model assets required by the current runtime."""
    return {
        "megadetector_checkpoint": DETECTOR_CHECKPOINT_PATH,
        "megadetector_config": DETECTOR_CONFIG_PATH,
        "keypoints": KEYPOINTS_MODEL_PATH,
        "identify_embedder": EMBEDDER_MODEL_PATH,
        "species_classifier": SPECIES_MODEL_PATH,
        "flank_side_classifier": SIDE_MODEL_PATH,
    }


CONFIG_PATH = DATA_DIR / "config.json"

HOST = os.environ.get("PUGMARK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PUGMARK_PORT", "7860"))

_SYNC_SECRET_FILE = DATA_DIR / "sync_secret.txt"

def _load_sync_secret() -> str:
    """Load the HMAC shared secret for sync bundles.
    Priority: env var PUGMARK_SYNC_SECRET > data/sync_secret.txt > auto-generate.
    The file is created on first access and must be copied to other
    range-office laptops to enable cross-node bundle sync.
    """
    from_env = os.environ.get("PUGMARK_SYNC_SECRET", "")
    if from_env:
        return from_env
    if _SYNC_SECRET_FILE.exists():
        try:
            val = _SYNC_SECRET_FILE.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    # Auto-generate and persist secret so sync is ready out of the box
    try:
        import secrets
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        _SYNC_SECRET_FILE.write_text(secret, encoding="utf-8")
        return secret
    except Exception:
        return "pugmark-reserve-sync-v1"


def get_sync_secret() -> str:
    global SYNC_SECRET
    if not SYNC_SECRET:
        SYNC_SECRET = _load_sync_secret()
    return SYNC_SECRET


def set_sync_secret(secret: str) -> str:
    global SYNC_SECRET
    secret = secret.strip()
    if not secret:
        import secrets
        secret = secrets.token_urlsafe(32)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SYNC_SECRET_FILE.write_text(secret, encoding="utf-8")
    except OSError:
        pass
    SYNC_SECRET = secret
    return SYNC_SECRET


SYNC_SECRET: str = _load_sync_secret()


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

    scan_batch_size: int = 2000
    """Maximum file records held by one ingest scan batch.  This bounds
    scanner/queue memory on a 500k-frame SD-card import; it is deliberately
    independent of model-inference batch size."""

    metadata_bytes_per_image: int = 2048
    """Conservative SQLite/JSON bookkeeping allowance used by the resource
    preflight.  It is an estimate stated to the operator, not a disk quota."""

    decode_working_set_mib: int = 64
    """RAM reserved for decoding a single high-resolution image while a scan
    batch is resident.  Tune from measurements on the release laptop."""

    ram_safety_reserve_mib: int = 512
    """Free RAM that remains unavailable to PUGMARK's scan so the operating
    system and the officer's other work do not begin paging heavily."""

    min_free_disk_mib: int = 1024
    """Absolute free-space floor in addition to the card-sized quarantine and
    metadata projection.  The preflight refuses before writing below it."""


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
class QualityEngine:
    """Image and crop quality gate policy."""
    min_laplacian_variance: float = 20.0  # Blur detection
    min_mean_exposure: float = 18.0       # Underexposure floor
    max_mean_exposure: float = 242.0      # Overexposure ceiling
    max_highlight_saturation_frac: float = 0.30  # IR flash blowout limit
    min_crop_pixels: int = 4096
    nms_iou_threshold: float = 0.45
    burst_fusion_enabled: bool = True


@dataclass
class StationMatching:
    """Multi-signal station identifier scoring weights."""
    weight_folder_hint: float = 0.40
    weight_camera_serial: float = 0.25
    weight_make_model: float = 0.15
    weight_filename_pattern: float = 0.10
    weight_deployment_history: float = 0.10
    min_confidence_to_auto_assign: float = 0.75


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

    cross_flank_window_s: int = 60
    """Two opposite-side captures within this time window at the same station
    are marked as cross-flank candidates (UNKNOWN_RELATIONSHIP) rather than
    auto-enrolling a second individual."""


@dataclass
class Classifiers:
    """Policy for the two local TorchScript classifiers.

    Species and physical flank side are independent evidence. Their
    confidence is never substituted for pose/keypoint quality.
    """

    species_min_confidence: float = 0.80
    side_min_confidence: float = 0.85
    """Calibrated, not guessed -- earlier revisions of the shipped side
    classifier had confidence that did not track correctness at all (a
    threshold sweep found ~22-30% precision at every threshold from 0 to
    0.85, i.e. the model's confidence carried no real signal). Retrained
    with a corrected label set (tools/build_atrw_side_manifest.py's
    multi-keypoint-pair vote, not shoulder+hip alone) and a backbone
    initialised from the local re-ID embedder rather than trained from
    scratch (tools/train_classifiers.py --arch pretrained). On the held-out
    validation split this now behaves like a real classifier: precision
    rises from 71.5% at threshold 0 to 76.9% at 0.85, and drops off past
    it (n too small to trust past 0.90). 0.85 sits at that precision peak,
    not merely inherited -- see edge/models/side/flank_side_classifier.ts.json
    for the confusion matrix this was measured from. Re-measure before
    moving this value again."""
    input_width: int = 224
    input_height: int = 224


@dataclass
class ClassifierTraining:
    """Offline-only training defaults for the Stage 2 TorchScript artifacts."""

    epochs: int = 24
    batch_size: int = 16
    learning_rate: float = 0.001
    seed: int = 20260815
    grayscale_probability: float = 0.25
    blur_radius_max: float = 1.5
    brightness_min: float = 0.45
    brightness_max: float = 1.35


@dataclass
class Device:
    """Field inference-device policy.

    Device selection is deliberately automatic and conservative: a field
    operator cannot accidentally force CUDA just by carrying over a shell
    variable from a development machine.  CUDA is used only when PyTorch can
    query a usable device with enough currently free VRAM; otherwise every
    model uses CPU.  These are throughput/recovery limits, not model
    thresholds, and are still configuration so a release can state them.
    """

    cpu_batch_size: int = 4
    cuda_batch_size: int = 16
    cuda_min_free_vram_mib: int = 1024
    cuda_oom_cpu_fallback: bool = True


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

    directional_trend_min_cycles: int = 2
    directional_trend_max_angle_diff_deg: float = 50.0
    village_proximity_warn_km: float = 2.5
    activity_collapse_ratio: float = 0.25
    travel_speed_max_kmh: float = 25.0
    id_confidence_collapse_drop: float = 0.25

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
class Auth:
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    """A session dies at whichever of these comes first. Independent of
    any background job -- edge/jobs.py runs on its own thread and does
    not read the session at all, so a job outlives a logout exactly as it
    outlives a browser tab closing."""

    failed_attempts_before_lock: int = 5
    lockout_minutes: tuple = (1, 5, 15, 30)
    """Index by (failed_attempts - failed_attempts_before_lock), clamped
    to the last entry -- progressively longer locks, never permanent, and
    never a full account disable (that stays a separate, admin-only,
    explicit action -- edge/db/repo.py::disable_user())."""

    min_password_length: int = 12
    recovery_code_groups: int = 6
    recovery_code_group_len: int = 4
    """A 24-character code in 4-character groups (XXXX-XXXX-...), from an
    alphabet with ambiguous characters (0/O, 1/I/l) removed -- it has to
    be legible off a printed sheet and typed back in by hand, offline,
    with no copy-paste guaranteed."""

    roles: tuple = ("field", "biologist", "director", "analyst", "admin")
    """Must match users.role's CHECK constraint (edge/db/migrations/
    0001_init.sql) exactly -- this is what admin user-creation validates
    against before the database's own constraint would reject it anyway,
    so the account-creation route can return a clear 400 instead of a
    500 from a raw IntegrityError."""


ALL_ROLES = ("admin", "director", "biologist", "field", "analyst")

PERMISSIONS = {
    "user_manage": ("admin",),
    "dev_seed": ALL_ROLES,
    "pipeline_trigger": ALL_ROLES,
    "review_decide": ALL_ROLES,
    "individual_promote": ALL_ROLES,
    "individual_merge": ALL_ROLES,
    "alert_ack": ALL_ROLES,
    "audit_read": ALL_ROLES,
    "sync_manage": ALL_ROLES,
    "ops_manage": ALL_ROLES,
    "ingest_manage": ALL_ROLES,
}


TERMINAL_STATUSES = (
    "INGESTED", "CORRUPT", "UNREADABLE", "BLANK", "PERSON", "VEHICLE",
    "NON_TARGET_SPECIES", "UNKNOWN_SPECIES", "SIDE_UNKNOWN", "LOW_QUALITY",
    "IDENTITY_REVIEW", "IDENTIFIED", "NEW_INDIVIDUAL", "DUPLICATE",
    "ERROR_RETRYABLE", "ERROR_PERMANENT"
)


@dataclass
class Config:
    ingest: Ingest = field(default_factory=Ingest)
    station_matching: StationMatching = field(default_factory=StationMatching)
    triage: Triage = field(default_factory=Triage)
    quality: QualityEngine = field(default_factory=QualityEngine)
    identify: Identify = field(default_factory=Identify)
    classifiers: Classifiers = field(default_factory=Classifiers)
    classifier_training: ClassifierTraining = field(default_factory=ClassifierTraining)
    device: Device = field(default_factory=Device)
    occupancy: Occupancy = field(default_factory=Occupancy)
    alerts: Alerts = field(default_factory=Alerts)
    privacy: Privacy = field(default_factory=Privacy)
    auth: Auth = field(default_factory=Auth)

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
    # MODELS_DIR is deliberately absent: the application never creates an
    # empty weights directory and never treats that as a reason to fetch.
    for d in (DATA_DIR, QUARANTINE_DIR, CROPS_DIR, RESTRICTED_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)


CONFIG = Config.load()
