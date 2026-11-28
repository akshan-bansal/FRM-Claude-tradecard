"""Quantum-state research engine: BSM_Spectral atoms -> coupled asset network -> volatility regimes.

Research only. Nothing in this package places, sizes or gates an order, and nothing here is
imported by the Router, the risk gates or any runner. Its outputs are forecasts that a
pre-registered benchmark judges; see ``reports/qhft/``.

Layers, each with an explicit computational definition (no physics is used as metaphor alone):

* ``statevector`` - exact simulation of ``RY(arccos x)`` encoding plus transverse-field Ising
  evolution ``exp(-iHt)``, ``H = sum J_ij Z_i Z_j + sum h_i Z_i + Gamma sum X_i`` (Schrodinger dynamics,
  superposition, energy conservation as an integrity check).
* ``field`` - the field equations. Static: ``m = tanh(h + J m)``, unique only when ``||J||_2 < 1``.
  Dynamic: mean-field Bloch equations, the classical field limit of the exact evolution. They scale
  to many nodes where the exact state (``2**n`` amplitudes) cannot.
* ``uncertainty`` - two rival direction/volatility relations: qubit complementarity with the BSM
  spot-vol angle ``phi = arccos(rho)``, and Christoffersen-Diebold ``P(up) = 1 - F(-mu/sigma)``.
* ``network`` - the two-level model: atoms -> each asset's local field -> learned asset couplings.
* ``panel`` / ``composite`` - concurrent walk-forward over a panel of assets, week-clustered
  bootstrap scoring, and log-linear pooling of the models' forecasts.
* ``atoms`` - point-in-time BSM_Spectral atoms per underlying.
* ``fingerprint`` - SHA-256 of a canonical encoding, binding every result to its inputs and config.
"""
from __future__ import annotations
