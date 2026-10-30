"""OASIS simulation layer: seed a run here, run it on a VM, ingest the result.

The simulation itself (CAMEL-AI OASIS, arXiv:2411.11581) runs on a separate VM under Python 3.11
(``scripts/oasis_vm_runner.py``), because ``camel-oasis`` needs Python < 3.12 and pins packages the trading
environment cannot share. This package is the repo side of that boundary: a spend ledger, the seed (an
OSINT briefing plus a hard budget), the result contract, and the ingest that turns a run into graph edges
and a society-stance record. Nothing here calls an LLM or spends money, and nothing in the trading path
imports it.
"""
from trading_live_claude.sim.budget import BudgetError, RunBudget, remaining
from trading_live_claude.sim.ingest import ingest
from trading_live_claude.sim.result import ResultError, SimResult, parse_result
from trading_live_claude.sim.seed import SimSeed, build_seed, load_seed, write_seed

__all__ = ["BudgetError", "ResultError", "RunBudget", "SimResult", "SimSeed", "build_seed", "ingest",
           "load_seed", "parse_result", "remaining", "write_seed"]
