"""Quantum kernel benchmark: angle-encoded feature map, fidelity kernel.

The kernel K(x, x') = |<phi(x)|phi(x')>|^2 is computed exactly from local
statevectors (free, unlimited) and validated on SV1 at real shot counts to
quantify shot noise. Benchmarked against linear and RBF-kernel logistic
regression on the edge-model labeled data; the quantum path is adopted only
if it wins on held-out Brier score.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("autotrader.quantum.kernel")

S3_BUCKET = "amazon-braket-465768367549-results"


def feature_circuit(x: np.ndarray):
    """ZZ-lite feature map: H, RY(2x_i) per qubit, ring CNOTs with RZ(2 x_i x_j)."""
    from braket.circuits import Circuit

    n = len(x)
    circ = Circuit()
    for i in range(n):
        circ.h(i)
        circ.ry(i, 2.0 * float(x[i]))
    for i in range(n):
        j = (i + 1) % n
        circ.cnot(i, j)
        circ.rz(j, 2.0 * float(x[i]) * float(x[j]))
        circ.cnot(i, j)
    return circ


def statevectors(X: np.ndarray) -> np.ndarray:
    """Exact statevector per sample via the local simulator."""
    from braket.devices import LocalSimulator

    sim = LocalSimulator()
    states = []
    for row in X:
        circ = feature_circuit(row)
        circ.state_vector()
        res = sim.run(circ).result()
        states.append(np.asarray(res.values[0], dtype=complex))
    return np.vstack(states)


def fidelity_kernel(states: np.ndarray) -> np.ndarray:
    """K = |<psi_i|psi_j>|^2 for all pairs."""
    gram = states @ states.conj().T
    return np.abs(gram) ** 2


def _kernel_ridge_brier(K_train: np.ndarray, y_train: np.ndarray,
                        K_val: np.ndarray, y_val: np.ndarray,
                        *, lam: float = 1.0) -> float:
    """Kernel ridge regression on labels in {0,1}; Brier on validation."""
    n = K_train.shape[0]
    A = K_train + lam * np.eye(n)
    alpha = np.linalg.solve(A, y_train)
    p = np.clip(K_val @ alpha, 0.0, 1.0)
    return float(np.mean((p - y_val) ** 2))


def _rbf_kernel(X: np.ndarray, gamma: float | None = None) -> np.ndarray:
    if gamma is None:
        gamma = 1.0 / X.shape[1]
    sq = np.sum(X**2, axis=1)
    d2 = sq[:, None] + sq[None, :] - 2.0 * (X @ X.T)
    return np.exp(-gamma * np.maximum(d2, 0.0))


def _rbf_kernel_cross(Xa: np.ndarray, Xb: np.ndarray, gamma: float) -> np.ndarray:
    sa = np.sum(Xa**2, axis=1)
    sb = np.sum(Xb**2, axis=1)
    d2 = sa[:, None] + sb[None, :] - 2.0 * (Xa @ Xb.T)
    return np.exp(-gamma * np.maximum(d2, 0.0))


def benchmark_kernels(
    X_train: np.ndarray, y_train: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray,
    *, sv1_check_pairs: int = 0, sv1_shots: int = 1000,
) -> dict[str, Any]:
    """Compare linear / RBF / quantum-fidelity kernels on held-out Brier."""
    from autotrader.edge_model import _fit_logistic, _sigmoid, _brier

    out: dict[str, Any] = {}
    # Linear logistic baseline.
    w, b = _fit_logistic(X_train, y_train)
    out["linear_brier"] = round(_brier(_sigmoid(X_val @ w + b), y_val), 4)

    # RBF kernel ridge baseline.
    gamma = 1.0 / X_train.shape[1]
    K_tr = _rbf_kernel(X_train, gamma)
    K_va = _rbf_kernel_cross(X_val, X_train, gamma)
    out["rbf_brier"] = round(_kernel_ridge_brier(K_tr, y_train, K_va, y_val), 4)

    # Quantum fidelity kernel (exact local statevectors).
    states_tr = statevectors(X_train)
    states_va = statevectors(X_val)
    Kq_tr = fidelity_kernel(states_tr)
    gram_va = states_va @ states_tr.conj().T
    Kq_va = np.abs(gram_va) ** 2
    out["quantum_brier"] = round(
        _kernel_ridge_brier(Kq_tr, y_train, Kq_va, y_val), 4
    )
    out["winner"] = min(
        ("linear", "rbf", "quantum"), key=lambda k: out[f"{k}_brier"]
    )

    # Optional SV1 shot-noise validation on a few random pairs.
    if sv1_check_pairs > 0:
        out["sv1_checks"] = _sv1_validate(
            X_train, states_tr, pairs=sv1_check_pairs, shots=sv1_shots
        )
    return out


def _sv1_validate(
    X: np.ndarray, states: np.ndarray, *, pairs: int, shots: int,
) -> list[dict[str, Any]]:
    """Run overlap circuits on SV1: <0..0| phi(xj)^dag phi(xi) |0..0>."""
    from braket.aws import AwsDevice

    sv1 = AwsDevice("arn:aws:braket:::device/quantum-simulator/amazon/sv1")
    rng = np.random.default_rng(7)
    checks = []
    n = len(X)
    for _ in range(pairs):
        i, j = int(rng.integers(n)), int(rng.integers(n))
        circ_i = feature_circuit(X[i])
        circ_j = feature_circuit(X[j])
        circ = circ_i + circ_j.adjoint()
        task = sv1.run(
            circ, shots=shots,
            s3_destination_folder=(S3_BUCKET, "kernel-validation"),
        )
        counts = task.result().measurement_counts
        p0 = counts.get("0" * len(X[i]), 0) / shots
        exact = float(np.abs(states[i].conj() @ states[j]) ** 2)
        checks.append({
            "i": i, "j": j, "shots": shots,
            "sv1_overlap": round(p0, 4), "exact_overlap": round(exact, 4),
            "abs_err": round(abs(p0 - exact), 4),
        })
    return checks
