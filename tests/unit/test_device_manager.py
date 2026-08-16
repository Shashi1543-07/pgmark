"""Unit tests for Stage 3's shared device selection and OOM recovery.

Run with ``python -m tests.unit.test_device_manager``.  No GPU, model asset,
or network connection is required; CUDA is represented by a DevicePlan only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from edge import config  # noqa: E402
from edge.pipeline.device import DeviceManager, DevicePlan, is_cuda_oom  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(f"{name}{' -- ' + detail if detail else ''}")


def _cuda_manager() -> DeviceManager:
    manager = DeviceManager(config.Device(cpu_batch_size=3, cuda_batch_size=4,
                                          cuda_min_free_vram_mib=0))
    manager._plan = DevicePlan(torch.device("cuda:0"), 4, 4096, 8192, "test CUDA")
    return manager


def test_oom_detection_is_narrow() -> None:
    check("CUDA allocation error is recognised", is_cuda_oom(RuntimeError("CUDA out of memory")))
    check("ordinary model error is not misreported as OOM", not is_cuda_oom(RuntimeError("bad logits")))
    check("CPU allocation error does not trigger CUDA fallback",
          not is_cuda_oom(RuntimeError("out of memory")))


def test_batch_halving_preserves_order_then_uses_cpu() -> None:
    manager = _cuda_manager()
    calls = []

    def operation(items, device):
        calls.append((list(items), device.type))
        if device.type == "cuda":
            raise RuntimeError("CUDA out of memory")
        return [f"cpu-{item}" for item in items]

    result = manager.run_batches([1, 2, 3, 4], operation)
    check("OOM recovery returns every item in original order",
          result == ["cpu-1", "cpu-2", "cpu-3", "cpu-4"], str(result))
    check("CUDA batch is halved before CPU is tried",
          calls[:3] == [([1, 2, 3, 4], "cuda"), ([1, 2], "cuda"), ([1], "cuda")], str(calls))
    check("single-item CUDA OOM falls back to CPU",
          any(items == [1] and device == "cpu" for items, device in calls), str(calls))


def test_cpu_batches_are_bounded_and_do_not_hide_errors() -> None:
    manager = DeviceManager(config.Device(cpu_batch_size=2, cuda_batch_size=4,
                                          cuda_min_free_vram_mib=0))
    manager._plan = DevicePlan(torch.device("cpu"), 2, None, None, "test CPU")
    calls = []

    def operation(items, device):
        calls.append((list(items), device.type))
        return list(items)

    check("CPU path splits work into configured bounded batches",
          manager.run_batches([1, 2, 3, 4, 5], operation) == [1, 2, 3, 4, 5]
          and calls == [([1, 2], "cpu"), ([3, 4], "cpu"), ([5], "cpu")], str(calls))


def main() -> int:
    test_oom_detection_is_narrow()
    test_batch_halving_preserves_order_then_uses_cpu()
    test_cpu_batches_are_bounded_and_do_not_hide_errors()
    print("\n".join(f"  ok   {item}" for item in PASS))
    if FAIL:
        print("\n".join(f"  FAIL {item}" for item in FAIL))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
