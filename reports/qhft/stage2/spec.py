"""The stage-2 constants. Its fingerprint is written into PREREGISTRATION_DRAFT.md and checked at freeze and at run."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from trading_live_claude.quantum.fingerprint import of_model
from trading_live_claude.quantum.panel import WalkForwardSpec

# Eight BSM_Spectral underlyings, one qubit each. SPX is a node (its atoms feed the couplings) but is
# not scored in the primary family: its weeks were seen in stage 1.
KEYS: tuple[str, ...] = ("SPX", "NDX", "DJIA", "WTI", "BTC", "ETH", "EURUSD", "USDJPY")
SCORED: tuple[int, ...] = tuple(i for i, k in enumerate(KEYS) if k != "SPX")
PRIMARY_LEVEL = 0.975    # two primaries, Bonferroni over a 95% family
SECONDARY_LEVEL = 0.95

SPEC = WalkForwardSpec(
    first_eval=104, refit_every=13, network_refit_every=26,
    models=("B", "uncoupled", "field", "quantum", "composite"),
    baseline_l2=1.0, network_l2=1.0, max_norm=0.95, quantum_bound=1.0, gamma=0.4, t=1.0,
    network_maxiter=1000, quantum_maxiter=200, composite_min_weeks=26, min_train_labels=30,
    boot_block=8, boot_reps=5000, boot_seed=20261128,
)

if __name__ == "__main__":
    print(of_model(SPEC))
