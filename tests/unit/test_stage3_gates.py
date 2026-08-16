"""Safety tests for Stage 3 gates without model weights or a database."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge import config, imageio  # noqa: E402
from edge.pipeline import classifiers, stage3  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' -- ' + detail if detail else ''}")


DET = {"det_id": "det_test", "orig_path": "not-read.jpg", "x": 0.1, "y": 0.1,
       "w": 0.5, "h": 0.5}
RUN = {"reserve_id": "TEST"}


def _run_with(species: classifiers.Classification, side: classifiers.Classification | None):
    originals = {
        "read": imageio.read_bgr,
        "species": stage3.classify_species,
        "side": stage3.classify_flank_side,
        "set_species": stage3.repo_ext.set_detection_species,
        "terminal": stage3._terminal_crop,
        "queue": stage3._queue_review,
        "embedder": stage3.identify.load_embedder,
    }
    calls = {"side": 0, "embedder": 0, "queue": 0, "species": None}
    try:
        imageio.read_bgr = lambda _path: np.zeros((100, 100, 3), dtype=np.uint8)
        stage3.classify_species = lambda _det, _image: species
        def side_call(_det, _image):
            calls["side"] += 1
            return side
        stage3.classify_flank_side = side_call
        stage3.repo_ext.set_detection_species = lambda *args, **kwargs: calls.__setitem__("species", args[1])
        stage3._terminal_crop = lambda *_args, **_kwargs: "crop_test"
        stage3._queue_review = lambda *_args, **_kwargs: calls.__setitem__("queue", calls["queue"] + 1)
        def no_embedder(_path):
            calls["embedder"] += 1
            raise AssertionError("identity model must not load before both gates pass")
        stage3.identify.load_embedder = no_embedder
        outcome, model = stage3._process_detection_checked(
            DET, RUN, None, {}, config.CONFIG.identify, "tiger", "test", object())
        return outcome, model, calls
    finally:
        imageio.read_bgr = originals["read"]
        stage3.classify_species = originals["species"]
        stage3.classify_flank_side = originals["side"]
        stage3.repo_ext.set_detection_species = originals["set_species"]
        stage3._terminal_crop = originals["terminal"]
        stage3._queue_review = originals["queue"]
        stage3.identify.load_embedder = originals["embedder"]


def test_species_gate() -> None:
    outcome, model, calls = _run_with(
        classifiers.Classification("leopard", 0.99, "classifier", "species@test"), None)
    check("a confirmed non-target species never invokes side or identity inference",
          outcome == "non_target_species" and model is None and calls["side"] == 0
          and calls["embedder"] == 0 and calls["species"] == "leopard", str(calls))

    outcome, model, calls = _run_with(
        classifiers.Classification("unknown", None, "unavailable", None), None)
    check("unknown species is reviewable but never enters identity inference",
          outcome == "unknown_species" and model is None and calls["side"] == 0
          and calls["embedder"] == 0 and calls["queue"] == 1, str(calls))


def test_side_gate() -> None:
    outcome, model, calls = _run_with(
        classifiers.Classification("tiger", 0.99, "classifier", "species@test"),
        classifiers.Classification("UNKNOWN", 0.60, "classifier", "side@test"))
    check("a tiger with unknown flank side is queued without loading the identity model",
          outcome == "side_unknown" and model is None and calls["side"] == 1
          and calls["embedder"] == 0 and calls["queue"] == 1, str(calls))


def main() -> int:
    test_species_gate()
    test_side_gate()
    print("\n".join(f"  ok   {item}" for item in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {item}" for item in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
