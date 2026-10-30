"""Optional NVIDIA GPU metrics via NVML (``pip install restrack[gpu]``).

psutil has no GPU support; this module degrades to "no GPUs" when NVML or a
driver is missing, so importing it is always safe.
"""
from __future__ import annotations

from typing import Any, Dict, List

try:  # pragma: no cover - depends on hardware
    import pynvml  # provided by the nvidia-ml-py package
except Exception:  # noqa: BLE001
    pynvml = None


class NvidiaGPU:
    def __init__(self) -> None:
        self.available = False
        self._handles: list = []
        if pynvml is None:
            return
        try:  # pragma: no cover - depends on hardware
            pynvml.nvmlInit()
            n = pynvml.nvmlDeviceGetCount()
            self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(n)]
            self.available = n > 0
        except Exception:  # noqa: BLE001
            self.available = False

    def sample(self) -> List[Dict[str, Any]]:  # pragma: no cover - depends on hardware
        out = []
        for i, h in enumerate(self._handles):
            def get(fn, *a):
                try:
                    return fn(h, *a)
                except Exception:  # noqa: BLE001
                    return None

            name = get(pynvml.nvmlDeviceGetName)
            if isinstance(name, bytes):
                name = name.decode()
            util = get(pynvml.nvmlDeviceGetUtilizationRates)
            mem = get(pynvml.nvmlDeviceGetMemoryInfo)
            temp = get(pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU)
            power = get(pynvml.nvmlDeviceGetPowerUsage)
            out.append({
                "index": i,
                "name": name or f"GPU {i}",
                "util_percent": util.gpu if util else None,
                "mem_util_percent": util.memory if util else None,
                "mem_used": mem.used if mem else None,
                "mem_total": mem.total if mem else None,
                "temperature": temp,
                "power_w": round(power / 1000.0, 1) if power is not None else None,
            })
        return out
