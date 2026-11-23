"""Reset promotion state after a gate rebase so stale 25%/80% champions
do not block or fake-advance the funding ladder.

    python -m autotrader.reset_promotion_state

Clears: champion.gate_passed (forces re-eval), hard_goal_reached,
consecutive_paper_passes, funding_rung, gene_bounds (so new genes can
explore), and optionally the champion blob entirely.
"""

from __future__ import annotations

import argparse
import json
import sys

from autotrader.daemon import load_state, save_state
from autotrader.strategy_space import reset_gene_bounds


def reset_promotion(*, drop_champion: bool = False, clear_gene_bounds: bool = True) -> dict:
    state = load_state()
    state["hard_goal_reached"] = False
    state["consecutive_paper_passes"] = 0
    state["funding_rung"] = 0
    if clear_gene_bounds:
        state["gene_bounds"] = {}
        reset_gene_bounds()
    champ = state.get("champion")
    if champ and not drop_champion:
        # Keep the genome as a seed, but force re-scoring under the new gate.
        champ["gate_passed"] = False
        champ["oos_pass_rate"] = 0.0
        champ.pop("stretch_25_pass_rate", None)
        champ.pop("stretch_25_passed", None)
        state["champion"] = champ
    elif drop_champion:
        state["champion"] = None
    state["promotion_reset_note"] = (
        "Promotion state cleared for operational gate rebase (3%/30%). "
        "gene_bounds reset for expanded DNA search."
    )
    save_state(state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset autotrader promotion state")
    parser.add_argument("--drop-champion", action="store_true")
    parser.add_argument("--keep-gene-bounds", action="store_true")
    args = parser.parse_args(argv)
    state = reset_promotion(
        drop_champion=args.drop_champion,
        clear_gene_bounds=not args.keep_gene_bounds,
    )
    print(json.dumps({
        "cycle": state.get("cycle"),
        "champion": (state.get("champion") or {}).get("gid"),
        "hard_goal_reached": state.get("hard_goal_reached"),
        "gene_bounds_cleared": not args.keep_gene_bounds,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
