"""The same network as a Qiskit circuit, for hardware or noisy-simulator runs later.

Qiskit is optional (``uv sync --extra quantum``). The exact simulator in ``statevector`` is the
reference. This first-order Trotter circuit converges to it as ``steps`` grows, which a test pins.
Hardware adds queue time and noise. The stage-1 benchmark found nothing that hardware could rescue,
so no runtime submission lives here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from .statevector import Edge


def trotter_circuit(x: npt.ArrayLike, couplings: Mapping[Edge, float], gamma: float, t: float,
                    steps: int, fields: Sequence[float] | None = None) -> Any:
    """``RY(arccos x)`` encoding, then ``steps`` first-order Trotter layers of RZ, RZZ and RX."""
    try:
        from qiskit import QuantumCircuit
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError("qiskit is optional: install it with `uv sync --extra quantum`") from exc
    xa = np.asarray(x, dtype=np.float64)
    if xa.ndim != 1 or np.any(np.abs(xa) > 1.0) or steps < 1:
        raise ValueError("x must be one row of atoms in [-1, 1], and steps >= 1")
    n = len(xa)
    qc = QuantumCircuit(n)
    for i, v in enumerate(xa):
        qc.ry(float(np.arccos(v)), i)
    dt = t / steps
    for _ in range(steps):
        if fields is not None:
            for i, f in enumerate(fields):
                qc.rz(2.0 * f * dt, i)
        for (i, j), v in couplings.items():
            qc.rzz(2.0 * v * dt, i, j)
        for i in range(n):
            qc.rx(2.0 * gamma * dt, i)
    return qc
