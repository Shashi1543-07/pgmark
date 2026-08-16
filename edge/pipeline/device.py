"""Shared, conservative local inference-device policy.

CUDA is optional acceleration, never a runtime requirement.  A field run uses
CPU when CUDA is absent, cannot be inspected, has insufficient free VRAM, or
reports an allocation failure.  This module never contacts a network service.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from edge import config

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class DevicePlan:
    device: Any
    batch_size: int
    free_vram_mib: int | None
    total_vram_mib: int | None
    detail: str

    @property
    def is_cuda(self) -> bool:
        if isinstance(self.device, str):
            return self.device.startswith("cuda")
        return getattr(self.device, "type", "") == "cuda"


def _mib(value: int | float) -> int:
    return int(value // (1024 * 1024))


def is_cuda_oom(exc: BaseException) -> bool:
    """Match allocation failures only; application errors must still surface."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).lower()
    return "cuda" in message and "out of memory" in message


class DeviceManager:
    """Inspect CUDA once and run ordered inference batches safely.

    Actual model inference is the capacity probe. A CUDA batch that OOMs is
    retried as two halves; an OOM at one item falls back to CPU when enabled.
    Guessing VRAM from model size would ignore source-image shape and allocator
    state, so it is intentionally not used as a safety decision.
    """

    def __init__(self, policy: config.Device | None = None):
        self.policy = policy or config.CONFIG.device
        self._plan: DevicePlan | None = None

    def plan(self) -> DevicePlan:
        if self._plan is None:
            self._plan = self._inspect()
        return self._plan

    def _cpu(self, detail: str) -> DevicePlan:
        try:
            import torch
            dev = torch.device("cpu")
        except Exception:
            dev = "cpu"
        return DevicePlan(dev, max(1, int(self.policy.cpu_batch_size)),
                          None, None, detail)

    def _inspect(self) -> DevicePlan:
        try:
            import torch
        except Exception:
            return self._cpu("PyTorch not installed; using CPU")

        try:
            if not torch.cuda.is_available():
                return self._cpu("CUDA unavailable; using CPU")
            free_bytes, total_bytes = torch.cuda.mem_get_info(0)
            free_mib, total_mib = _mib(free_bytes), _mib(total_bytes)
            minimum = max(0, int(self.policy.cuda_min_free_vram_mib))
            if free_mib < minimum:
                return self._cpu(
                    f"CUDA has {free_mib} MiB free, below configured {minimum} MiB; using CPU")
            name = torch.cuda.get_device_name(0)
            return DevicePlan(torch.device("cuda:0"), max(1, int(self.policy.cuda_batch_size)),
                              free_mib, total_mib,
                              f"CUDA {name}: {free_mib}/{total_mib} MiB free at selection")
        except Exception as exc:  # broken drivers must not block a field run
            return self._cpu(f"CUDA inspection failed ({type(exc).__name__}); using CPU")

    def clear_cuda_cache(self) -> None:
        if self.plan().is_cuda:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    def run_one(self, operation: Callable[[Any], R]) -> R:
        plan = self.plan()
        try:
            return operation(plan.device)
        except BaseException as exc:
            if not (plan.is_cuda and self.policy.cuda_oom_cpu_fallback and is_cuda_oom(exc)):
                raise
            self.clear_cuda_cache()
            try:
                import torch
                cpu_dev = torch.device("cpu")
            except Exception:
                cpu_dev = "cpu"
            return operation(cpu_dev)

    def run_batches(self, items: Sequence[T],
                    operation: Callable[[Sequence[T], Any], Sequence[R]]) -> list[R]:
        """Return exactly one ordered result per item, or raise visibly."""
        if not items:
            return []
        plan = self.plan()
        out: list[R] = []
        for start in range(0, len(items), max(1, plan.batch_size)):
            out.extend(self._run_chunk(items[start:start + plan.batch_size], plan.device, operation))
        return out

    def _run_chunk(self, items: Sequence[T], device: Any,
                   operation: Callable[[Sequence[T], Any], Sequence[R]]) -> list[R]:
        try:
            result = list(operation(items, device))
            if len(result) != len(items):
                raise RuntimeError(
                    f"inference batch returned {len(result)} results for {len(items)} inputs")
            return result
        except BaseException as exc:
            dev_type = getattr(device, "type", str(device))
            if not (dev_type.startswith("cuda") and is_cuda_oom(exc)):
                raise
            self.clear_cuda_cache()
            if len(items) > 1:
                middle = max(1, len(items) // 2)
                return (self._run_chunk(items[:middle], device, operation)
                        + self._run_chunk(items[middle:], device, operation))
            if getattr(self.policy, "cuda_oom_cpu_fallback", True):
                try:
                    import torch
                    cpu_dev = torch.device("cpu")
                except Exception:
                    cpu_dev = "cpu"
                return self._run_chunk(items, cpu_dev, operation)
            raise


_manager: DeviceManager | None = None


def get_device_manager() -> DeviceManager:
    global _manager
    if _manager is None:
        _manager = DeviceManager()
    return _manager


def reset_device_manager() -> None:
    """Test helper; production keeps one warm process-wide manager."""
    global _manager
    _manager = None
