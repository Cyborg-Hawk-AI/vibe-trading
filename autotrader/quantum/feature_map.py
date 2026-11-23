"""Numpy-native statevector for the ZZ-lite quantum feature map.

Identical circuit to ``kernel.feature_circuit`` (H, RY(2x) per qubit, ring
CNOT-RZ(2 x_i x_j)-CNOT) but computed directly on the statevector — ~100x
faster than the Braket local simulator and dependency-free at predict time,
which is what makes the quantum kernel usable inside the nightly edge-model
model-class search and the live hunter path.

Verified against ``kernel.statevectors`` in tests.
"""

from __future__ import annotations

import numpy as np


def feature_statevector(x: np.ndarray) -> np.ndarray:
    """Exact statevector of the ZZ-lite feature map for one sample."""
    n = len(x)
    dim = 1 << n
    # Start from |0..0>, apply H to all qubits: uniform superposition.
    state = np.full(dim, 1.0 / np.sqrt(dim), dtype=complex)
    # RY(2 x_i) per qubit.
    for i in range(n):
        state = _apply_ry(state, i, n, 2.0 * float(x[i]))
    # Ring ZZ: CNOT(i,j) RZ_j(2 x_i x_j) CNOT(i,j) == exp(-i theta/2 Z_i Z_j)
    # up to phase convention; apply as parity-phase kick.
    for i in range(n):
        j = (i + 1) % n
        theta = 2.0 * float(x[i]) * float(x[j])
        state = _apply_zz_phase(state, i, j, n, theta)
    return state


def _apply_ry(state: np.ndarray, qubit: int, n: int, theta: float) -> np.ndarray:
    c = np.cos(theta / 2.0)
    s = np.sin(theta / 2.0)
    dim = 1 << n
    idx = np.arange(dim)
    bit = (idx >> (n - 1 - qubit)) & 1
    out = state.copy()
    zero = bit == 0
    one = ~zero
    # |0> -> c|0> + s|1>; |1> -> -s|0> + c|1|  (RY convention)
    partner0 = idx[one] & ~(1 << (n - 1 - qubit))
    out[zero] = c * state[zero] - s * state[idx[one]]
    out[one] = s * state[partner0] + c * state[one]
    return out


def _apply_zz_phase(state: np.ndarray, q1: int, q2: int, n: int, theta: float) -> np.ndarray:
    """exp(-i theta/2 * Z_q1 Z_q2): exp(-i theta/2) when bits agree (zz=+1)."""
    idx = np.arange(1 << n)
    b1 = (idx >> (n - 1 - q1)) & 1
    b2 = (idx >> (n - 1 - q2)) & 1
    zz = np.where((b1 ^ b2) == 0, 1.0, -1.0)
    return state * np.exp(-0.5j * theta * zz)


def statevectors_fast(X: np.ndarray) -> np.ndarray:
    return np.vstack([feature_statevector(row) for row in X])


def fidelity_kernel_fast(states: np.ndarray) -> np.ndarray:
    gram = states @ states.conj().T
    return np.abs(gram) ** 2


def kernel_against(states_a: np.ndarray, states_b: np.ndarray) -> np.ndarray:
    """K[i, j] = |<a_i|b_j>|^2."""
    return np.abs(states_a @ states_b.conj().T) ** 2
