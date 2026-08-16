"""Measures Stage B (MegaDetector V6, MDV6-mit-yolov9-c) throughput on
CPU -- "processing throughput on constrained hardware" is an explicit
jury criterion (CLAUDE.md), and until this ran, the only number for it
was a size comparison (9.7M params vs MDv5's 139.9M), not a measured
rate.

    python -m tools.bench_detector [--n 200]

Requires edge/models/megadetector/ from the verified offline release bundle
and data/raw/atrw/train/ (tools.fetch_data --set atrw) for real sample
images -- this is a throughput measurement against real photographs, not
synthetic frames, since JPEG decode time is part of what gets measured.

Reports images/sec after a warmup pass (the first inference call pays a
one-time cost -- lazy model construction, PyTorch's own graph/kernel
warmup -- that does not repeat on frame 2 onward and would understate
steady-state throughput if counted).
"""
from __future__ import annotations

import argparse
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch                                    # noqa: E402

from edge import config                         # noqa: E402
from edge.pipeline import detector as detector_pipeline   # noqa: E402
from edge.pipeline.device import get_device_manager       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    if not (detector_pipeline.CHECKPOINT_PATH.exists() and detector_pipeline.CONFIG_PATH.exists()):
        print("missing megadetector weights -- run "
              "the verified offline model bundle first")
        return 1

    atrw_train = Path("data/raw/atrw/train")
    if not atrw_train.exists():
        print("missing data/raw/atrw/train -- run "
              "`python -m tools.fetch_data --set atrw` first")
        return 1

    images = sorted(atrw_train.glob("*.jpg"))[:args.n]
    if not images:
        print("no sample images found")
        return 1

    plan = get_device_manager().plan()
    print(f"CPU: {platform.processor()}")
    print(f"torch threads: {torch.get_num_threads()} (torch's own default for this machine, "
          f"not pinned by this script)")
    print(f"selected device: {plan.device} ({plan.detail})")
    print(f"configured inference batch size: {plan.batch_size}")
    print(f"images: {len(images)}")

    det = detector_pipeline.get_detector()   # lazy load, excluded from the timed region

    warmup_n = min(5, len(images))
    det.detect_many([str(image) for image in images[:warmup_n]],
                    conf_threshold=config.CONFIG.triage.detector_conf_threshold)

    t0 = time.time()
    results = det.detect_many([str(image) for image in images],
                              conf_threshold=config.CONFIG.triage.detector_conf_threshold)
    elapsed = time.time() - t0
    if any(result is None for result in results):
        print("one or more benchmark images could not be read; no throughput result recorded")
        return 1

    rate = len(images) / elapsed
    print(f"\n{len(images)} images in {elapsed:.2f}s -- "
          f"{rate:.2f} images/sec ({1000/rate:.1f} ms/image)")
    print(f"a 20,000-frame survey cycle at this rate: {20_000/rate/60:.1f} minutes of "
          f"Stage B alone, single-threaded-equivalent wall clock on this machine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
