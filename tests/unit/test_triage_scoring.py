"""Unit tests: pure logic, no models, no I/O. See blueprint §13 Layer 1.

    python -m tests.unit.test_triage_scoring

This is the test AUDIT_AND_REVISED_PLAN.md P0-2 says is worth more than
any number on a slide: a direct answer to "how do you know you aren't
throwing away tigers?" It exists because the live suite's own triage
fixture (tests/fixtures/triage_corpus.py) used a subject covering a
quarter of the frame -- large enough that it could never have caught the
bug this file is built to catch: a small, low-contrast subject silently
averaged away and quarantined as blank.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from edge import config                       # noqa: E402
from edge.pipeline.triage import cell_score   # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def _old_mean_score(grid: np.ndarray, background: np.ndarray) -> float:
    """The scoring this test exists to have caught, kept only so the
    'this would have failed before, passes now' claim is proven here
    rather than just asserted in prose."""
    return float(np.mean(np.abs(grid.astype(float) - background.astype(float)))) / 255.0


def test_small_subject_is_not_scored_as_blank() -> None:
    """The audit's own worst-case table (AUDIT_AND_REVISED_PLAN.md P0-2):
    a subject occupying as little as 1 of 256 cells, at contrast as low
    as 12 out of 255 -- night infrared against a warm background is
    routinely this subtle -- must never score at or below the blank
    threshold."""
    bg = np.full((16, 16), 100.0)
    threshold = config.CONFIG.triage.stage_a_blank_threshold
    old_threshold = 0.012   # the value in force before this fix

    old_failures = 0
    for contrast in (12, 25, 60, 140):
        for cells in (1, 2, 4, 8):
            flat = bg.copy().flatten()
            flat[:cells] = 100.0 + contrast
            frame = flat.reshape(16, 16)

            new_score = cell_score(frame, bg)
            check(f"cells={cells} contrast={contrast}: scores above the blank "
                  f"threshold ({new_score:.4f} > {threshold})",
                  new_score > threshold, f"score={new_score:.4f}")

            if _old_mean_score(frame, bg) <= old_threshold:
                old_failures += 1

    check("the old mean-based scoring would have missed at least one of these "
          "cases -- proving this test would have caught the original bug",
          old_failures > 0, f"{old_failures}/16 cases scored as blank under the old logic")


def test_a_genuinely_blank_frame_still_scores_low() -> None:
    """The fix must not be so aggressive that ordinary sensor/compression
    noise between two truly empty frames reads as 'contains a subject' --
    that would defeat Stage A's entire purpose of removing real blanks
    cheaply. A few points of gentle per-cell noise should stay well
    under threshold."""
    rng = np.random.default_rng(20260813)
    bg = np.full((16, 16), 100.0)
    noisy = bg + rng.normal(0, 1.5, size=(16, 16))
    score = cell_score(noisy, bg)
    threshold = config.CONFIG.triage.stage_a_blank_threshold
    check("ordinary background noise between two blank frames stays under threshold",
          score <= threshold, f"score={score:.4f}, threshold={threshold}")


def main() -> int:
    test_small_subject_is_not_scored_as_blank()
    test_a_genuinely_blank_frame_still_scores_low()
    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
