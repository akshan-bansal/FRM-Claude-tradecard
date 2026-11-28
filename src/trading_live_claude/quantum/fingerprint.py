"""SHA-256 fingerprints that bind every result to the exact inputs and configuration it came from.

This is the "encode state as cryptography" layer, defined narrowly:
- A fingerprint proves *which* state was evaluated. It cannot recover the state, so the state itself
  is kept beside it.
- A pre-registration freezes a config fingerprint before any data is seen. A run records the input
  and config fingerprints it used, so a result can be matched to its frozen protocol.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel


def of_array(a: npt.NDArray[Any]) -> str:
    """Hash of dtype, shape and the C-ordered bytes: equal arrays hash equal, reshapes do not."""
    arr = np.ascontiguousarray(a)
    h = hashlib.sha256(f"{arr.dtype.str}|{arr.shape}|".encode())
    h.update(arr.tobytes())
    return h.hexdigest()


def of_json(obj: Any) -> str:
    """Hash of canonical JSON (sorted keys, no whitespace)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def of_model(model: BaseModel) -> str:
    return of_json(model.model_dump(mode="json"))


def combine(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
