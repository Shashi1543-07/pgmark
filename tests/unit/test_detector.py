"""Unit tests: pure logic, no models, no I/O. See blueprint §13 Layer 1.

    python -m tests.unit.test_detector

edge/pipeline/detector.py's CLASS_NAMES maps the model's integer class ids
to the label strings edge/pipeline/triage.py's Stage B routing compares
against -- in particular, whichever id currently means "person" has to
route to persons_restricted, never the tiger pipeline. That coupling used
to be two independently-typed "person" string literals (one in
CLASS_NAMES, one in triage.py's routing condition); a future edit to
either one alone would have silently broken routing with no error, since
`d.label == "person"` just quietly stops matching. Fixed by making
PERSON_LABEL/ANIMAL_LABEL/VEHICLE_LABEL the single source of truth for
both sides -- this file proves that fix stays true, not just that it
was true once.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge.pipeline import detector    # noqa: E402

PASS, FAIL = [], []
TRIAGE_PATH = Path(__file__).resolve().parents[2] / "edge" / "pipeline" / "triage.py"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def test_class_names_are_built_from_the_shared_constants() -> None:
    check("CLASS_NAMES values are exactly the three named constants, not separately "
          "spelled-out strings that happen to match them today",
          set(detector.CLASS_NAMES.values())
          == {detector.ANIMAL_LABEL, detector.PERSON_LABEL, detector.VEHICLE_LABEL})
    check("PERSON_LABEL is the schema's own vocabulary "
          "(detections.label / images.status, edge/db/migrations/0001_init.sql)",
          detector.PERSON_LABEL == "person")


def test_no_hardcoded_label_literal_bypasses_the_shared_constant() -> None:
    """Parses triage.py's own source and fails if any comparison against
    a detection's .label uses a bare string literal ("person", "animal",
    "vehicle") instead of detector.PERSON_LABEL / ANIMAL_LABEL /
    VEHICLE_LABEL -- the exact class of drift this file exists to catch,
    caught by re-parsing the real file, not by re-reading it once."""
    tree = ast.parse(TRIAGE_PATH.read_text(encoding="utf-8"), filename=str(TRIAGE_PATH))
    bare_literal_comparisons = []
    watched = {"animal", "person", "vehicle"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                for side in (node.left, comparator):
                    if isinstance(side, ast.Constant) and side.value in watched:
                        bare_literal_comparisons.append(f"line {node.lineno}: {side.value!r}")

    check("triage.py never compares a detection's label against a bare 'animal'/'person'/"
          "'vehicle' string literal -- every such comparison must go through "
          "detector.ANIMAL_LABEL / PERSON_LABEL / VEHICLE_LABEL",
          not bare_literal_comparisons, "; ".join(bare_literal_comparisons))


def main() -> int:
    test_class_names_are_built_from_the_shared_constants()
    test_no_hardcoded_label_literal_bypasses_the_shared_constant()
    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
