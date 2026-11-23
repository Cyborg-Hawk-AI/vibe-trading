"""Quantum-assisted research tooling (AWS Braket).

Honest scope note (verified 2026-08-31): D-Wave annealers are no longer on
Braket, so large QUBOs are solved by the classical simulated annealer here;
gate-model quantum (SV1) is benchmarked on tractable instances (<=30 vars
QAOA, quantum kernels <=30 qubits) and kept only where it beats classical.
"""
