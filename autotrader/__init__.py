"""Autotrader — autonomous simulation, mutation, and validation pipeline.

Layers Vibe-Trading's backtest engines/loaders (deterministic inner loop)
with an evolutionary strategy-mutation outer loop, deterministic trade
diagnostics, optional LLM-assisted mutation proposals, and an optional
TradingAgents committee review of finalists.

Entry point: ``python -m autotrader.optimize_loop`` (or ``run_pipeline.sh``).
"""

__version__ = "0.1.0"
