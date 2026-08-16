"""Evaluate a bundled Stage 2 TorchScript classifier against local JSONL data.

The manifest format is the one used by ``tools.train_classifiers``. Evaluation
uses the runtime loader and thresholds, so it proves the exact offline model
path and logits contract that the field pipeline will execute.

    python -m tools.eval_classifiers --task species --manifest data/local/species/manifest.jsonl
    python -m tools.eval_classifiers --task side --manifest data/local/side/acceptance.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from edge.pipeline import classifiers
from tools.train_classifiers import _labels, _rows


def evaluate(task: str, manifest: Path) -> dict:
    rows = [row for row in _rows(manifest, task) if row.split == "val"]
    if not rows:
        raise ValueError("manifest has no validation rows")
    labels = _labels(task)
    inverse = {index: label for index, label in enumerate(labels)}
    matrix = {label: {other: 0 for other in labels} for label in labels}
    unavailable: list[str] = []
    tagged: dict[str, dict[str, int]] = {}
    for row in rows:
        image = cv2.imread(str(row.path))
        if image is None:
            raise RuntimeError(f"cannot decode local validation image {row.path}")
        result = (classifiers.classify_species(image, (0.0, 0.0, 1.0, 1.0))
                  if task == "species"
                  else classifiers.classify_side(image, (0.0, 0.0, 1.0, 1.0)))
        expected = inverse[row.label]
        if result.source != "classifier":
            unavailable.append(result.detail or str(row.path))
            continue
        matrix[expected][result.label] += 1
        for tag in row.tags:
            tagged.setdefault(tag, {"total": 0, "correct": 0})
            tagged[tag]["total"] += 1
            tagged[tag]["correct"] += int(result.label == expected)
    if unavailable:
        raise RuntimeError("runtime classifier unavailable; no score is valid:\n  "
                           + "\n  ".join(unavailable[:10]))
    total = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[label][label] for label in labels)
    report = {
        "task": task, "labels": list(labels), "total": total,
        "accuracy": correct / total if total else 0.0, "confusion_matrix": matrix,
        "challenge_tags": {tag: {**counts, "accuracy": counts["correct"] / counts["total"]}
                           for tag, counts in sorted(tagged.items())},
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=("species", "side"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = evaluate(args.task, args.manifest.resolve())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"offline classifier evaluation refused: {exc}")
        return 2
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.report}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
