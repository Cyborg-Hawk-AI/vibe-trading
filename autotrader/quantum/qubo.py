"""QUBO builders and solvers for feature selection and capital allocation.

Problem forms (minimize x^T Q x, x in {0,1}^n):

- Feature selection: maximize relevance-to-label minus redundancy, with a
  cardinality penalty at exactly k features:
      Q_ii = -rel_i + mu(1 - 2k)      Q_ij = 2*mu + lambda*|corr_ij|
- Capital allocation: maximize edge minus covariance risk over at most k
  positions of equal budget units:
      Q_ii = -edge_i + mu(1 - 2k)     Q_ij = 2*mu + lambda*cov_ij

Solvers: vectorized simulated annealing (production, any n) and a QAOA-on-SV1
benchmark path for n <= 30 (cost-gated; see runner.py).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger("autotrader.quantum.qubo")

QAOA_MAX_VARS = 30  # SV1 practical ceiling for p-layer QAOA


def build_feature_selection_qubo(
    X: np.ndarray, y: np.ndarray, k: int, *, lam: float = 1.0, mu: float | None = None,
) -> np.ndarray:
    """QUBO for selecting exactly k of n features (relevance - redundancy)."""
    n = X.shape[1]
    std = X.std(axis=0)
    Xs = (X - X.mean(axis=0)) / np.where(std < 1e-9, 1.0, std)
    Xs[:, std < 1e-9] = 0.0  # constant columns carry no signal
    yc = y - y.mean()
    rel = np.abs(Xs.T @ yc) / max(float(np.sqrt((yc**2).sum())), 1e-9)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.abs(np.corrcoef(Xs.T))
    corr = np.where(np.isfinite(corr), corr, 0.0)
    np.fill_diagonal(corr, 0.0)
    if mu is None:
        # Cardinality must dominate worst-case redundancy: C(k,2) pairs * lam.
        mu = float(lam * k * k + rel.max() + 1e-3)
    Q = np.zeros((n, n))
    for i in range(n):
        Q[i, i] = -rel[i] + mu * (1 - 2 * k)
        for j in range(i + 1, n):
            Q[i, j] = 2 * mu + lam * corr[i, j]
    return Q


def build_allocation_qubo(
    edge: np.ndarray, cov: np.ndarray, k: int, *, lam: float = 1.0,
    mu: float | None = None,
) -> np.ndarray:
    """QUBO for picking exactly k equal-unit positions: edge - lambda*risk."""
    n = len(edge)
    if mu is None:
        mu = float(lam * k * k + np.abs(edge).max() + 1e-9)
    Q = np.zeros((n, n))
    for i in range(n):
        Q[i, i] = -edge[i] + mu * (1 - 2 * k)
        for j in range(i + 1, n):
            Q[i, j] = 2 * mu + lam * cov[i, j]
    return Q


def qubo_energy(Q: np.ndarray, x: np.ndarray) -> float:
    """E = x^T Q x with Q upper-triangular (off-diagonal counted once)."""
    return float(x @ Q @ x)


def solve_qubo_annealing(
    Q: np.ndarray, *, restarts: int = 64, sweeps: int = 2000, seed: int = 42,
) -> dict[str, Any]:
    """Vectorized simulated annealing over all restarts simultaneously.

    Q is upper-triangular: E = sum_i Q_ii x_i + sum_{i<j} Q_ij x_i x_j.
    Pair field uses M = Q + Q.T so each unordered pair contributes once.
    """
    n = Q.shape[0]
    rng = np.random.default_rng(seed)
    M = Q + Q.T
    diag = np.diag(Q).copy()
    x = rng.integers(0, 2, size=(restarts, n)).astype(float)
    e = np.einsum("bi,ij,bj->b", x, Q, x)
    best_x, best_e = x.copy(), e.copy()
    t0, t1 = 2.0, 1e-3
    for s in range(sweeps):
        temp = t0 * (t1 / t0) ** (s / max(sweeps - 1, 1))
        flip = rng.integers(0, n, size=restarts)
        rows = x[np.arange(restarts), flip]
        # dE = (1-2x_b)(Q_bb + sum_{j!=b} M_bj x_j); (M@x)_b includes 2 Q_bb x_b.
        pair_field = np.take_along_axis(x @ M, flip[:, None], axis=1)[:, 0] - 2 * diag[flip] * rows
        delta = (1 - 2 * rows) * (diag[flip] + pair_field)
        accept = (delta < 0) | (rng.random(restarts) < np.exp(-delta / max(temp, 1e-9)))
        x[np.arange(restarts)[accept], flip[accept]] = 1 - rows[accept]
        e = np.where(accept, e + delta, e)
        better = e < best_e
        best_x[better] = x[better]
        best_e[better] = e[better]
    i = int(np.argmin(best_e))
    return {
        "x": best_x[i].astype(int).tolist(),
        "energy": float(best_e[i]),
        "solver": "simulated_annealing",
        "restarts": restarts,
        "sweeps": sweeps,
    }


def qubo_to_ising(Q: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Upper-tri QUBO -> (h, J, offset) with x = (1 + z)/2, E = h.z + z.J.z + c."""
    Qfull = Q + Q.T - np.diag(np.diag(Q))
    n = Q.shape[0]
    J = np.zeros((n, n))
    h = np.zeros(n)
    offset = 0.0
    for i in range(n):
        h[i] += Qfull[i, i] / 2.0
        offset += Qfull[i, i] / 2.0
        for j in range(i + 1, n):
            J[i, j] = Qfull[i, j] / 4.0
            h[i] += Qfull[i, j] / 4.0
            h[j] += Qfull[i, j] / 4.0
            offset += Qfull[i, j] / 4.0
    return h, J, offset


def solve_qubo_qaoa_sv1(
    Q: np.ndarray,
    *,
    p: int = 2,
    shots: int = 1000,
    angle_steps: int = 6,
    s3_folder: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """QAOA benchmark on SV1 for small QUBOs (n <= QAOA_MAX_VARS).

    Coarse grid over (gamma, beta) per layer pair (shared across layers) —
    not a full optimizer; this is a benchmark probe, not production solving.
    Cost: (angle_steps^2) tasks x shots each on SV1.
    """
    n = Q.shape[0]
    if n > QAOA_MAX_VARS:
        raise ValueError(f"QAOA path limited to {QAOA_MAX_VARS} vars, got {n}")
    from braket.aws import AwsDevice
    from braket.circuits import Circuit

    h, J, offset = qubo_to_ising(Q)
    sv1 = AwsDevice("arn:aws:braket:::device/quantum-simulator/amazon/sv1")

    best: dict[str, Any] = {"energy": math.inf}
    tasks_run = 0
    for gamma in np.linspace(0.1, math.pi, angle_steps):
        for beta in np.linspace(0.1, math.pi / 2, angle_steps):
            circ = Circuit()
            for i in range(n):
                circ.h(i)
            for _layer in range(p):
                for i in range(n):
                    if abs(h[i]) > 1e-12:
                        circ.rz(i, 2 * gamma * h[i])
                for i in range(n):
                    for j in range(i + 1, n):
                        if abs(J[i, j]) > 1e-12:
                            circ.cnot(i, j).rz(j, 2 * gamma * J[i, j]).cnot(i, j)
                for i in range(n):
                    circ.rx(i, 2 * beta)
            kwargs = {"shots": shots}
            if s3_folder:
                kwargs["s3_destination_folder"] = s3_folder
            task = sv1.run(circ, **kwargs)
            counts = task.result().measurement_counts
            tasks_run += 1
            for bitstr, cnt in counts.items():
                x = np.array([int(b) for b in reversed(bitstr)], dtype=float)
                e = float(x @ Q @ x)
                if e < best["energy"]:
                    best = {
                        "energy": e, "x": x.astype(int).tolist(),
                        "gamma": round(float(gamma), 3), "beta": round(float(beta), 3),
                        "count": cnt,
                    }
    best.update({
        "solver": "qaoa_sv1", "p": p, "shots": shots,
        "angle_steps": angle_steps, "tasks_run": tasks_run,
    })
    return best
