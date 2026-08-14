"""Unit tests: pure logic, no models, no I/O. See blueprint §13 Layer 1.

    python -m tests.unit.test_vendor_licensing

Two different, deliberately different, licence boundaries in this
codebase, and this file checks both stay where they were put:

    Stage B (edge/pipeline/detector.py + edge/pipeline/vendor/yolo_mit/)
        must NEVER depend on ultralytics (AGPL-3.0) -- see
        vendor/yolo_mit/NOTICE.md for why it is vendored, MIT-licensed
        code instead of `pip install PytorchWildlife`.

    The keypoint regressor (edge/pipeline/keypoints.py,
        tools/train_keypoints.py) is trained WITH Ultralytics YOLO11-pose,
        deliberately -- docs/MODEL_CHOICES.md records this as an accepted
        trade for a government forest-department deployment, where a
        copyleft licence on the source is acceptable and arguably
        desirable. requirements.txt is expected to declare ultralytics
        for this reason; that is not the same mistake Stage B avoided,
        and this file must not conflate the two.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS, FAIL = [], []

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_ROOT = REPO_ROOT / "edge" / "pipeline" / "vendor" / "yolo_mit"
DETECTOR_PATH = REPO_ROOT / "edge" / "pipeline" / "detector.py"

# Anything under these top-level module names is forbidden in Stage B
# specifically: ultralytics and yolov5 are AGPL-3.0 (PytorchWildlife's
# own declared dependencies, unconditionally, regardless of model variant
# -- see NOTICE.md); supervision/wget/lightning are the parts of
# PyTorchWildlife's own wrapper this build deliberately did NOT vendor,
# because single-image inference against a pre-downloaded checkpoint does
# not need them, and their presence would mean something copied more of
# the upstream package than intended. This list applies ONLY to Stage B
# (detector.py and its vendor tree) -- edge/pipeline/keypoints.py and its
# training script are explicitly exempt; see the module docstring.
FORBIDDEN_IN_STAGE_B = {"ultralytics", "yolov5", "supervision", "wget", "lightning"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}{' — ' + detail if detail else ''}")


def _imported_top_level_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:   # level > 0 is a relative import (.yolo_mit_base etc)
                modules.add(node.module.split(".")[0])
    return modules


def test_vendor_tree_exists() -> None:
    check("the vendored yolo_mit tree exists where detector.py expects it",
          VENDOR_ROOT.is_dir(), str(VENDOR_ROOT))


def test_no_forbidden_imports_in_stage_b() -> None:
    py_files = sorted(VENDOR_ROOT.rglob("*.py")) + [DETECTOR_PATH]
    check("Stage B actually contains Python files to scan (detector.py plus "
          "the vendored tree) -- an empty glob would make the check below "
          "vacuously true",
          len(py_files) > 1, str(py_files))

    violations: list[str] = []
    for f in py_files:
        found = _imported_top_level_modules(f) & FORBIDDEN_IN_STAGE_B
        if found:
            label = f.name if f == DETECTOR_PATH else str(f.relative_to(VENDOR_ROOT))
            violations.append(f"{label}: {sorted(found)}")

    check(f"none of {sorted(FORBIDDEN_IN_STAGE_B)} are imported anywhere in Stage B "
          f"(detector.py + {len(py_files) - 1} vendored files) -- checked by parsing "
          "every import statement, not by grepping for the string",
          not violations, "; ".join(violations))


def test_requirements_txt_scopes_ultralytics_to_the_pose_model() -> None:
    """ultralytics IS expected in requirements.txt now (the keypoint
    regressor uses it, deliberately) -- what matters is that it is
    accompanied by the AGPL note this file's docstring describes, not
    that it is absent. Absence would actually be a regression: it would
    mean keypoints.py's real (non-stub) implementation cannot load its
    own trained model on a machine that only installed requirements.txt."""
    req_path = REPO_ROOT / "requirements.txt"
    req_text = req_path.read_text()
    dep_lines = [line.split("#", 1)[0].strip().lower()
                 for line in req_text.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    declared = any(line.startswith("ultralytics") for line in dep_lines)
    check("requirements.txt declares ultralytics (for the keypoint regressor, "
          "not Stage B)", declared, str(dep_lines))
    check("requirements.txt's ultralytics line is accompanied by an AGPL note "
          "nearby, not a silent dependency",
          declared and "agpl" in req_text.lower())


def main() -> int:
    test_vendor_tree_exists()
    test_no_forbidden_imports_in_stage_b()
    test_requirements_txt_scopes_ultralytics_to_the_pose_model()
    print("\n".join(f"  ok   {p}" for p in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {f}" for f in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
