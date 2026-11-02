"""OASIS simulation layer: seed a run here, run it on a VM, ingest the result.

The simulation itself (CAMEL-AI OASIS, arXiv:2411.11581) runs on a separate VM under Python 3.11
(``scripts/oasis_vm_runner.py``), because ``camel-oasis`` needs Python < 3.12 and pins packages this
environment cannot share. This package is the repo side of that boundary: a spend ledger, the seed (an
OSINT briefing plus a hard budget), the result contract, and the ingest that turns a run into graph edges
and a society-stance record. Nothing here calls an LLM or spends money.

Two seed/result models live here. ``budget`` / ``seed`` / ``result`` / ``ingest`` are the VM path above and
own the package-level names. ``contract`` is the overlay-seeded boundary the society layer builds on; its
``RunBudget``, ``SimSeed``, ``SimResult`` and ``build_seed`` are imported from ``sim.contract``.

``society`` is the pure layer on top of a validated result: it turns the reported stances into
``holds_stance`` graph edges, maps a stance to an influence multiplier in (0, 1] that can only reduce size,
and runs the round-trip-cost check on the scaled intent. It places nothing. ``Router.society_view`` (off
unless set) is the one seam through which a :class:`SocietyView` reaches an entry.
"""
from trading_live_claude.sim.budget import BudgetError, RunBudget, remaining
from trading_live_claude.sim.ingest import ingest
from trading_live_claude.sim.result import ResultError, SimResult, parse_result
from trading_live_claude.sim.seed import SimSeed, build_seed, load_seed, write_seed

from .contract import ingest_result
from .journal import is_journaled, journal_run, poll_id_for, run_edges
from .oasis_graph import (
    OasisRun,
    attention_matrix,
    herd_from_run,
    oasis_edges,
    read_oasis_db,
)
from .society import (
    AdapterParams,
    SocietyView,
    compose,
    cost_check_after_scaling,
    society_edges,
    society_influence,
    society_scaled_edge,
    top_eigenvalue_share,
)

__all__ = ["AdapterParams", "BudgetError", "OasisRun", "ResultError", "RunBudget", "SimResult", "SimSeed",
           "SocietyView", "attention_matrix", "build_seed", "compose", "cost_check_after_scaling",
           "herd_from_run", "ingest", "ingest_result", "is_journaled", "journal_run", "load_seed",
           "oasis_edges", "parse_result", "poll_id_for", "read_oasis_db", "remaining", "run_edges",
           "society_edges", "society_influence", "society_scaled_edge", "top_eigenvalue_share",
           "write_seed"]
