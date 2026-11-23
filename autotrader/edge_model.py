"""Edge model: P(target-before-stop) from our own tape.

Nightly walk-forward logistic regression (numpy-native, no sklearn) that
learns which setups hit their hunter brackets. Artifact:
``autotrader/state/edge_model.json``.

Live path: ``predict_edge(features)`` returns ``(p, ev, size_frac)`` or
``None`` when no usable model exists. Calibration guardrail: if live Brier
degrades past the bootstrap baseline, sizing influence collapses to flat
0.9 while the EV gate remains.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np

from autotrader.bootstrap import ensure_agent_path, state_dir

ensure_agent_path()

logger = logging.getLogger("autotrader.edge_model")

ROOT = Path(__file__).resolve().parents[1]
LIVE_STATE = ROOT / "livetrader" / "state"
ARTIFACT_NAME = "edge_model.json"
LABELED_NAME = "edge_labels.jsonl"

FEATURE_KEYS = (
    "gap", "rvol", "buzz", "catalyst", "ext_vwap", "minutes_open", "side_short",
    "arch_gap_and_go", "arch_gap_and_fade", "arch_midday_pump",
    "arch_afternoon_fade", "arch_chop", "buzz_lead_min",
)
TARGET_PCT = 0.05
STOP_PCT = 0.035
TIME_BARS = 8
FRICTION = 0.006   # ~round-trip on cheap stocks
MIN_TRAIN = 80
BRIER_DEGRADE = 0.05
MIN_BASE_RATE = 0.02


def _artifact_path() -> Path:
    return state_dir() / ARTIFACT_NAME


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _features_row(d: dict[str, Any]) -> np.ndarray:
    return np.array([
        float(d.get("gap", 0.0) or 0.0),
        float(d.get("rvol", 0.0) or 0.0),
        float(d.get("buzz", 0.0) or 0.0),
        1.0 if d.get("catalyst") else float(d.get("catalyst", 0.0) or 0.0),
        float(d.get("ext_vwap", 0.0) or 0.0),
        float(d.get("minutes_open", 0.0) or 0.0) / 390.0,  # normalize to session
        float(d.get("side_short", 0.0) or 0.0),
        float(d.get("arch_gap_and_go", 0.0) or 0.0),
        float(d.get("arch_gap_and_fade", 0.0) or 0.0),
        float(d.get("arch_midday_pump", 0.0) or 0.0),
        float(d.get("arch_afternoon_fade", 0.0) or 0.0),
        float(d.get("arch_chop", 0.0) or 0.0),
        min(float(d.get("buzz_lead_min", 999.0) or 999.0), 240.0) / 240.0,
    ], dtype=float)


def _label_path_on_bars(
    close: np.ndarray, high: np.ndarray, low: np.ndarray, i: int, *,
    side: str = "long",
) -> tuple[int, float, float]:
    """Return (hit_target_first, mfe, mae) from bar i over TIME_BARS."""
    entry = float(close[i])
    if entry <= 0 or i + 1 >= len(close):
        return 0, 0.0, 0.0
    if side == "short":
        target = entry * (1.0 - TARGET_PCT)
        stop = entry * (1.0 + STOP_PCT)
        mfe = mae = 0.0
        for j in range(i + 1, min(i + 1 + TIME_BARS, len(close))):
            mfe = max(mfe, (entry - float(low[j])) / entry)
            mae = max(mae, (float(high[j]) - entry) / entry)
            if high[j] >= stop:
                return 0, mfe, mae
            if low[j] <= target:
                return 1, mfe, mae
        return 0, mfe, mae
    target = entry * (1.0 + TARGET_PCT)
    stop = entry * (1.0 - STOP_PCT)
    mfe = mae = 0.0
    for j in range(i + 1, min(i + 1 + TIME_BARS, len(close))):
        mfe = max(mfe, (float(high[j]) - entry) / entry)
        mae = max(mae, (entry - float(low[j])) / entry)
        if low[j] <= stop:
            return 0, mfe, mae
        if high[j] >= target:
            return 1, mfe, mae
    return 0, mfe, mae


# ---------------------------------------------------------------------------
# Labeling
# ---------------------------------------------------------------------------

def label_snapshots(day: str | None = None) -> list[dict[str, Any]]:
    """Join live setup snapshots against the day's 5m tape."""
    from livetrader.engine.snapshots import load_snapshots
    from autotrader.data import load_snapshot, snapshot_for_interval
    from autotrader.intraday_data import SNAPSHOT_BASE

    day = day or (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    snaps = load_snapshots(day)
    if not snaps:
        return []
    data = load_snapshot(snapshot_for_interval(SNAPSHOT_BASE, "5m"))
    target = np.datetime64(day)
    labeled: list[dict[str, Any]] = []
    for s in snaps:
        code = f"{s['symbol']}.US" if not str(s["symbol"]).endswith("-USDT") else s["symbol"]
        df = data.get(code)
        if df is None or df.empty:
            continue
        import pandas as pd
        from zoneinfo import ZoneInfo

        # Trust the bar timestamp over the filename: replay/test runs write
        # rows under today's filename with historical bar_ts, and joining on
        # the wrong day silently labels from the session's final bar.
        bar_ts = float(s.get("bar_ts", 0.0) or 0.0)
        if bar_ts <= 0:
            continue
        et = ZoneInfo("America/New_York")
        row_day = dt.datetime.fromtimestamp(bar_ts, tz=et).strftime("%Y-%m-%d")
        day_mask = pd.DatetimeIndex(df.index).normalize() == np.datetime64(row_day)
        day_df = df[day_mask]
        if len(day_df) < TIME_BARS + 2:
            continue
        # Nearest bar at/after snapshot bar_ts. The snapshot index is
        # ET-naive; localize before epoch conversion so it compares correctly
        # against the true epoch of bar_ts.
        idx = day_df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize(et)
        # Unit-safe epoch conversion: parquet frames may be datetime64[us]/[ms].
        epochs = idx.to_numpy(dtype="datetime64[s]").astype("int64").astype(float)
        i = int(np.searchsorted(epochs, bar_ts, side="left"))
        i = min(max(i, 0), len(day_df) - 2)
        close = day_df["close"].to_numpy(dtype=float)
        high = day_df["high"].to_numpy(dtype=float)
        low = day_df["low"].to_numpy(dtype=float)
        hit, mfe, mae = _label_path_on_bars(
            close, high, low, i, side=str(s.get("side", "long")),
        )
        labeled.append({
            **{k: s.get(k) for k in (
                "symbol", "side", "gap", "rvol", "buzz", "catalyst",
                "ext_vwap", "minutes_open", "taken", "skip_reason", "date_ny",
            )},
            "side_short": 1.0 if s.get("side") == "short" else 0.0,
            "label": hit,
            "mfe": round(mfe, 4),
            "mae": round(mae, 4),
            "source": "live_snapshot",
        })
    return labeled


def bootstrap_from_library(max_per_symbol: int = 80) -> list[dict[str, Any]]:
    """Synthesize labeled examples from the 60-day 5m replay library.

    Setup-like bars only; positives (target-before-stop) are kept exhaustively
    and negatives are downsampled ~3:1 so the logistic model has a learnable
    positive class from day one.
    """
    from autotrader.data import load_snapshot, snapshot_for_interval
    from autotrader.intraday_data import SNAPSHOT_BASE
    import pandas as pd

    data = load_snapshot(snapshot_for_interval(SNAPSHOT_BASE, "5m"))
    positives: list[dict[str, Any]] = []
    negatives: list[dict[str, Any]] = []
    for code, df in data.items():
        if not code.endswith(".US") or df is None or len(df) < 40:
            continue
        close = df["close"].to_numpy(dtype=float)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        vol = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else np.ones(len(close))
        idx = pd.DatetimeIndex(df.index)
        days = sorted(set(idx.normalize()))
        sampled = 0
        for day in days:
            mask = idx.normalize() == day
            if mask.sum() < TIME_BARS + 5:
                continue
            prev_days = [d for d in days if d < day]
            if not prev_days:
                continue
            prev = df[idx.normalize() == prev_days[-1]]
            if prev.empty:
                continue
            prev_close = float(prev["close"].iloc[-1])
            day_ix = np.where(mask)[0]
            day_close = close[day_ix]
            day_high = high[day_ix]
            day_low = low[day_ix]
            day_vol = vol[day_ix]
            tp = (day_high + day_low + day_close) / 3.0
            cum_pv = np.cumsum(tp * day_vol)
            cum_v = np.cumsum(np.maximum(day_vol, 1e-9))
            vwap = cum_pv / cum_v
            for local_i in range(3, len(day_ix) - TIME_BARS, 2):
                i = int(day_ix[local_i])
                px = float(close[i])
                if px <= 0 or prev_close <= 0:
                    continue
                gap = px / prev_close - 1.0
                med = float(np.median(day_vol[: local_i + 1])) or 1.0
                rvol = float(day_vol[local_i]) / med
                ext = px / float(vwap[local_i]) - 1.0 if vwap[local_i] > 0 else 0.0
                if abs(gap) < 0.03 and rvol < 2.0 and abs(ext) < 0.03:
                    continue
                minutes = local_i * 5.0
                for side in ("long", "short"):
                    hit, mfe, mae = _label_path_on_bars(
                        close, high, low, i, side=side,
                    )
                    row = {
                        "symbol": code.replace(".US", ""),
                        "side": side,
                        "gap": round(gap, 4),
                        "rvol": round(min(rvol, 20.0), 2),
                        "buzz": 0.0,
                        "catalyst": 0.0,
                        "ext_vwap": round(ext, 4),
                        "minutes_open": minutes,
                        "side_short": 1.0 if side == "short" else 0.0,
                        "label": hit,
                        "mfe": round(mfe, 4),
                        "mae": round(mae, 4),
                        "source": "bootstrap",
                        "date_ny": str(day.date()),
                    }
                    (positives if hit else negatives).append(row)
                sampled += 1
                if sampled >= max_per_symbol:
                    break
            if sampled >= max_per_symbol:
                break
    # Balance: keep all positives, sample ~3x negatives.
    rng = np.random.default_rng(42)
    n_neg = min(len(negatives), max(len(positives) * 3, 200))
    if n_neg and len(negatives) > n_neg:
        pick = rng.choice(len(negatives), size=n_neg, replace=False)
        negatives = [negatives[i] for i in pick]
    out = positives + negatives
    rng.shuffle(out)
    logger.info(
        "bootstrap labels: %d positives / %d negatives (from library)",
        len(positives), len(negatives),
    )
    return out


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------

def _fit_logistic(
    X: np.ndarray, y: np.ndarray, *, l2: float = 1.0, steps: int = 400, lr: float = 0.1,
) -> tuple[np.ndarray, float]:
    """Return (weights, bias). Features assumed already standardized."""
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(steps):
        p = _sigmoid(X @ w + b)
        err = p - y
        w -= lr * (X.T @ err / n + l2 * w)
        b -= lr * float(err.mean())
    return w, b


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _logits_from_proba(model: Any, X: np.ndarray) -> np.ndarray:
    """log-odds of an sklearn classifier's predict_proba output."""
    p = np.clip(model.predict_proba(X)[:, 1], 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def _sidecar_path() -> Path:
    return state_dir() / "edge_model_sklearn.pkl"


def _load_sidecar() -> Any | None:
    path = _sidecar_path()
    if not path.exists():
        return None
    try:
        import pickle

        return pickle.loads(path.read_bytes())
    except Exception:  # noqa: BLE001
        logger.warning("edge-model sidecar unreadable", exc_info=True)
        return None


def _platt_fit(
    logits: np.ndarray, y: np.ndarray, *, steps: int = 1000, lr: float = 0.2,
) -> tuple[float, float]:
    """Fit p = sigmoid(A*logit + C) on held-out data (Platt scaling).

    Uses Platt's smoothed targets to avoid fitting to 0/1 extremes. Logits
    are standardized internally for stable gradient steps, then mapped back.
    """
    n_pos = float(y.sum())
    n_neg = float(len(y) - n_pos)
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    t = np.where(y > 0.5, t_pos, t_neg)
    mu = float(logits.mean())
    sd = float(logits.std()) or 1.0
    z = (logits - mu) / sd
    a, c = 1.0, 0.0
    n = max(len(y), 1)
    for _ in range(steps):
        p = _sigmoid(a * z + c)
        err = p - t
        a -= lr * float(err @ z) / n
        c -= lr * float(err.sum()) / n
    return a / sd, c - a * mu / sd


def _news_ts_index(known_symbols: set[str] | None = None) -> dict[str, list[float]]:
    """symbol -> sorted headline timestamps from the persisted news log.

    Title tokens are only indexed when they match a known symbol (avoids
    word-ticker collisions like ALL/ARE/ON polluting the lead-lag feature).
    """
    from collections import defaultdict

    path = state_dir() / "news_log.jsonl"
    idx: dict[str, list[float]] = defaultdict(list)
    if not path.exists():
        return idx
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    h = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(h.get("ts", 0.0) or 0.0)
                if ts <= 0:
                    continue
                for s in h.get("symbols", []) or []:
                    idx[str(s).upper()].append(ts)
                if known_symbols:
                    title = str(h.get("title", "")).upper()
                    for token in title.split():
                        token = token.strip(".,:;!?'\"()")
                        if token in known_symbols:
                            idx[token].append(ts)
    except OSError:
        pass
    return {k: sorted(v) for k, v in idx.items()}


def _buzz_lead_from_index(idx: dict[str, list[float]], symbol: str, ts: float) -> float:
    import bisect

    from autotrader.edge_features import NO_BUZZ

    bare = symbol.split(".")[0].split("-")[0].upper()
    arr = idx.get(bare)
    if not arr:
        return NO_BUZZ
    i = bisect.bisect_right(arr, ts) - 1
    if i < 0:
        return NO_BUZZ
    # Same trading day only.
    if dt.datetime.fromtimestamp(arr[i]).date() != dt.datetime.fromtimestamp(ts).date():
        return NO_BUZZ
    return min((ts - arr[i]) / 60.0, NO_BUZZ)


def _enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add partial-archetype one-hots + buzz lead-lag to labeled rows.

    Archetype uses only bars up to the setup bar (no lookahead), matching
    what the live hunter can compute. Rows whose symbol-day is missing from
    the snapshot get archetype 'chop'.
    """
    from autotrader.data import load_snapshot
    from autotrader.edge_features import (
        NO_BUZZ, arch_onehots, partial_archetype,
    )

    symbols = {str(r.get("symbol", "")) for r in rows if r.get("symbol")}
    codes = [f"{s}.US" for s in sorted(symbols)] + sorted(symbols)
    try:
        data = load_snapshot("daytrade__5m", codes=codes)
    except Exception:  # noqa: BLE001
        logger.exception("enrichment snapshot load failed; archetypes default to chop")
        data = {}
    news_idx = _news_ts_index({s.split(".")[0].split("-")[0].upper() for s in symbols})
    day_cache: dict[tuple[str, str], Any] = {}
    enriched = 0
    for r in rows:
        sym = str(r.get("symbol", ""))
        date = str(r.get("date_ny", ""))
        minutes_open = float(r.get("minutes_open", 0.0) or 0.0)
        archetype = "chop"
        key = (sym, date)
        if key not in day_cache:
            frame = data.get(f"{sym}.US")
            if frame is None:
                frame = data.get(sym)
            day = None
            if frame is not None and date and len(frame):
                try:
                    day = frame[frame.index.strftime("%Y-%m-%d") == date]
                except Exception:  # noqa: BLE001
                    day = None
            day_cache[key] = day
        day = day_cache[key]
        if day is not None and len(day) >= 3:
            upto = min(int(minutes_open // 5), len(day) - 1)
            try:
                times = [
                    (ts.timetz().replace(tzinfo=None) if hasattr(ts, "timetz") else ts.time())
                    for ts in day.index
                ]
                archetype = partial_archetype(
                    times,
                    day["open"].astype(float).tolist() if "open" in day.columns else None,
                    day["close"].astype(float).tolist(),
                    upto,
                )
            except Exception:  # noqa: BLE001
                archetype = "chop"
        r.update(arch_onehots(archetype))
        # Setup time ≈ 9:30 ET + minutes_open (dates are NY session dates).
        try:
            open_dt = dt.datetime.strptime(date, "%Y-%m-%d") + dt.timedelta(
                hours=9, minutes=30 + minutes_open
            )
            r["buzz_lead_min"] = _buzz_lead_from_index(
                news_idx, sym, open_dt.timestamp()
            )
        except (ValueError, OverflowError):
            r["buzz_lead_min"] = NO_BUZZ
        enriched += 1
    logger.info("edge rows enriched with archetype+buzz-lead: %d", enriched)
    return rows


def _row_key(r: dict[str, Any]) -> tuple:
    return (
        r.get("symbol"), r.get("date_ny"), r.get("side"),
        round(float(r.get("minutes_open") or 0.0), 1),
        round(float(r.get("gap") or 0.0), 4),
    )


def train_edge_model(*, include_bootstrap: bool = True) -> dict[str, Any] | None:
    """Walk-forward train; write artifact. Returns the artifact dict."""
    rows = bootstrap_from_library() if include_bootstrap else []
    # Prefer yesterday's live labels when present.
    yesterday = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = label_snapshots(yesterday) + rows
    except Exception:  # noqa: BLE001
        logger.exception("live snapshot labeling failed; bootstrap only")

    # The accumulated labeled library dwarfs the bootstrap sample — merge it
    # in (deduped) so training scales with evidence instead of capping at 300.
    library = load_labeled()
    if library:
        have = {_row_key(r) for r in rows}
        rows.extend(r for r in library if "label" in r and _row_key(r) not in have)

    try:
        rows = _enrich_rows(rows)
    except Exception:  # noqa: BLE001 — enrichment is additive; never block train
        logger.exception("edge row enrichment failed; training on base features")

    if len(rows) < MIN_TRAIN:
        logger.warning("edge model: only %d labeled rows; skipping train", len(rows))
        return None

    # Chronological split: last 20% of unique dates as validation.
    dates = sorted({str(r.get("date_ny", "")) for r in rows if r.get("date_ny")})
    if len(dates) >= 5:
        cut = dates[max(1, int(len(dates) * 0.8))]
        train_rows = [r for r in rows if str(r.get("date_ny", "")) < cut]
        val_rows = [r for r in rows if str(r.get("date_ny", "")) >= cut]
    else:
        cut_i = int(len(rows) * 0.8)
        train_rows, val_rows = rows[:cut_i], rows[cut_i:]
    if len(train_rows) < MIN_TRAIN // 2 or len(val_rows) < 20:
        train_rows, val_rows = rows, rows[-max(20, len(rows) // 5):]

    X_train = np.vstack([_features_row(r) for r in train_rows])
    y_train = np.array([int(r["label"]) for r in train_rows], dtype=float)
    X_val = np.vstack([_features_row(r) for r in val_rows])
    y_val = np.array([int(r["label"]) for r in val_rows], dtype=float)

    mu = X_train.mean(axis=0)
    sd = X_train.std(axis=0)
    sd = np.where(sd < 1e-8, 1.0, sd)
    Xs = (X_train - mu) / sd
    Xv = (X_val - mu) / sd

    # Model-class search: logistic (numpy) vs small MLP vs gradient boosting,
    # each Platt-calibrated on the held-out split; the calibrated Brier picks
    # the winner. sklearn models persist to a sidecar pickle; the artifact
    # stays JSON.
    candidates: dict[str, dict[str, Any]] = {}
    w, b = _fit_logistic(Xs, y_train)
    candidates["logistic"] = {
        "logits_val": Xv @ w + b, "weights": w, "bias": float(b),
    }
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.neural_network import MLPClassifier

        mlp = MLPClassifier(
            hidden_layer_sizes=(16,), activation="relu", max_iter=400,
            random_state=42, early_stopping=True,
        )
        mlp.fit(Xs, y_train)
        candidates["mlp"] = {"sk_model": mlp, "logits_val": _logits_from_proba(mlp, Xv)}
        gbm = GradientBoostingClassifier(
            n_estimators=120, max_depth=2, learning_rate=0.08, random_state=42,
        )
        gbm.fit(Xs, y_train)
        candidates["gbm"] = {"sk_model": gbm, "logits_val": _logits_from_proba(gbm, Xv)}
    except ImportError:
        logger.info("sklearn unavailable; model-class search = logistic only")

    # Quantum fidelity-kernel ridge on the 7 base features (raw, matching the
    # validated benchmark convention). Exact numpy statevectors; train states
    # persist to the sidecar so the live path can score new setups.
    try:
        from autotrader.quantum.feature_map import (
            fidelity_kernel_fast, kernel_against, statevectors_fast,
        )

        # Bound kernel memory: cap train states at 4096 (134MB kernel matrix).
        qk_idx = np.arange(len(X_train))
        if len(qk_idx) > 4096:
            qk_idx = np.random.default_rng(42).choice(qk_idx, size=4096, replace=False)
        states_tr = statevectors_fast(X_train[qk_idx, :7])
        states_va = statevectors_fast(X_val[:, :7])
        K_tr = fidelity_kernel_fast(states_tr)
        K_va = kernel_against(states_va, states_tr)
        alpha = np.linalg.solve(K_tr + 1.0 * np.eye(len(K_tr)), y_train[qk_idx])
        scores = np.clip(K_va @ alpha, 1e-4, 1 - 1e-4)
        candidates["qkernel"] = {
            "logits_val": np.log(scores / (1 - scores)),
            "qk_states_train": states_tr,
            "qk_alpha": alpha,
        }
    except Exception as exc:  # noqa: BLE001 — quantum candidate is optional
        logger.info("qkernel candidate skipped: %s", exc)

    best_class, best = None, None
    for name, cand in candidates.items():
        logits_val = cand["logits_val"]
        pa, pc = _platt_fit(logits_val, y_val)
        p_cal = _sigmoid(pa * logits_val + pc)
        cand["platt_a"], cand["platt_c"] = float(pa), float(pc)
        cand["brier"] = _brier(p_cal, y_val)
        cand["brier_raw"] = _brier(_sigmoid(logits_val), y_val)
        cand["p_val"] = p_cal
        if best is None or cand["brier"] < best["brier"]:
            best_class, best = name, cand
    logger.info(
        "model-class search: %s",
        {n: round(c["brier"], 4) for n, c in candidates.items()},
    )
    w = best.get("weights")
    b = float(best.get("bias", 0.0))
    platt_a, platt_c = best["platt_a"], best["platt_c"]
    p_val = best["p_val"]
    brier = best["brier"]
    brier_raw = best["brier_raw"]
    # Constant base-rate predictor: the floor any usable model must beat.
    base_rate_val = float(y_val.mean()) if len(y_val) else 0.0
    brier_base = _brier(np.full_like(y_val, base_rate_val), y_val) if len(y_val) else 1.0
    # Usable only if calibrated Brier beats the constant predictor and stays
    # under an absolute ceiling — a model that fails this is worse than
    # predicting the base rate and must not gate real entries.
    usable = bool(len(y_val) >= 20 and brier <= 0.25 and brier <= 0.95 * brier_base)
    # Calibration bins (on calibrated probabilities)
    bins = []
    for lo in np.linspace(0, 1, 5, endpoint=False):
        hi = lo + 0.2
        mask = (p_val >= lo) & (p_val < hi)
        if mask.sum() < 5:
            continue
        bins.append({
            "lo": round(float(lo), 2), "hi": round(float(hi), 2),
            "mean_p": round(float(p_val[mask].mean()), 3),
            "mean_y": round(float(y_val[mask].mean()), 3),
            "n": int(mask.sum()),
        })

    # Label-distribution audit: per-source / per-side positive rates.
    label_stats: dict[str, Any] = {}
    for r in rows:
        key = f"{r.get('source', '?')}:{r.get('side', '?')}"
        st = label_stats.setdefault(key, {"n": 0, "pos": 0})
        st["n"] += 1
        st["pos"] += int(r.get("label", 0))
    label_stats = {
        k: {**v, "rate": round(v["pos"] / max(v["n"], 1), 4)}
        for k, v in sorted(label_stats.items())
    }

    artifact = {
        "trained_at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "n_train": len(train_rows),
        "n_val": len(val_rows),
        "model_class": best_class,
        "feature_keys": list(FEATURE_KEYS),
        "mu": mu.tolist(),
        "sd": sd.tolist(),
        "weights": w.tolist() if w is not None else [],
        "bias": float(b),
        "platt_a": float(platt_a),
        "platt_c": float(platt_c),
        "brier_val": round(brier, 4),
        "brier_val_raw": round(brier_raw, 4),
        "brier_base": round(brier_base, 4),
        "usable": usable,
        "calibration_bins": bins,
        "label_stats": label_stats,
        "base_rate": round(float(y_train.mean()), 4),
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "live_brier": None,
        "sizing_enabled": usable,
    }
    path = _artifact_path()
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    sidecar = _sidecar_path()
    if best.get("sk_model") is not None:
        import pickle

        sidecar.write_bytes(pickle.dumps(best["sk_model"]))
    elif best.get("qk_states_train") is not None:
        import pickle

        sidecar.write_bytes(pickle.dumps({
            "kind": "qkernel",
            "states_train": best["qk_states_train"],
            "alpha": best["qk_alpha"],
        }))
    elif sidecar.exists():
        sidecar.unlink()  # logistic won; drop any stale sidecar
    # Persist labeled rows for the hypothesis tester.
    lab = state_dir() / LABELED_NAME
    with lab.open("a", encoding="utf-8") as fh:
        for r in rows[:5000]:
            fh.write(json.dumps(r, default=str) + "\n")
    logger.info(
        "edge model trained: n_train=%d n_val=%d brier=%.3f (raw %.3f, base %.3f) "
        "base_rate=%.2f usable=%s labels=%s -> %s",
        len(train_rows), len(val_rows), brier, brier_raw, brier_base,
        artifact["base_rate"], usable, label_stats, path,
    )
    return artifact


# ---------------------------------------------------------------------------
# Live prediction
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] | None = None


def load_model(force: bool = False) -> dict[str, Any] | None:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    path = _artifact_path()
    if not path.exists():
        return None
    try:
        _CACHE = json.loads(path.read_text(encoding="utf-8"))
        return _CACHE
    except (json.JSONDecodeError, OSError):
        return None


def predict_edge(
    features: dict[str, Any],
    *,
    target_pct: float = TARGET_PCT,
    stop_pct: float = STOP_PCT,
) -> tuple[float, float, float] | None:
    """Return (p, ev, size_frac) or None if no usable model.

    size_frac is 0.5..0.9 when sizing is enabled; flat 0.9 when calibration
    has degraded (EV gate still applies via caller's check on ev).
    """
    model = load_model()
    if model is None or "weights" not in model:
        return None
    # Too-rare positive class => model collapses to "always reject". Fall back
    # to Phase-1 hunter behavior until live labeled setups accumulate.
    if float(model.get("base_rate") or 0.0) < MIN_BASE_RATE:
        return None
    # Models that failed validation (worse than the constant base-rate
    # predictor) must not gate or size real entries.
    if model.get("usable") is False:
        return None
    mu = np.array(model["mu"], dtype=float)
    sd = np.array(model["sd"], dtype=float)
    x = _features_row(features)
    xs = (x - mu) / sd
    model_class = model.get("model_class", "logistic")
    if model_class == "logistic":
        w = np.array(model["weights"], dtype=float)
        if len(w) != len(xs):
            return None
        logit = float(xs @ w + float(model["bias"]))
    elif model_class == "qkernel":
        payload = _load_sidecar()
        if not isinstance(payload, dict) or payload.get("kind") != "qkernel":
            return None
        from autotrader.quantum.feature_map import feature_statevector, kernel_against

        state = feature_statevector(x[:7])  # raw base features, as trained
        k = kernel_against(state.reshape(1, -1), payload["states_train"])[0]
        score = float(np.clip(k @ payload["alpha"], 1e-4, 1 - 1e-4))
        logit = float(np.log(score / (1 - score)))
    else:
        sk = _load_sidecar()
        if sk is None or not hasattr(sk, "predict_proba"):
            return None
        logit = float(_logits_from_proba(sk, xs.reshape(1, -1))[0])
    platt_a = float(model.get("platt_a", 1.0))
    platt_c = float(model.get("platt_c", 0.0))
    p = float(_sigmoid(np.array([platt_a * logit + platt_c]))[0])
    ev = p * target_pct - (1.0 - p) * stop_pct - FRICTION
    sizing_on = bool(model.get("sizing_enabled", True))
    live_b = model.get("live_brier")
    base_b = float(model.get("brier_val", 0.25))
    if live_b is not None and float(live_b) > base_b + BRIER_DEGRADE:
        sizing_on = False
    if sizing_on and ev > 0:
        # Fractional-Kelly-lite: map EV into 0.5..0.9 of book.
        edge_norm = min(ev / max(target_pct, 1e-6), 1.0)
        size_frac = 0.5 + 0.4 * edge_norm
    else:
        size_frac = 0.9
    return p, ev, size_frac


def update_live_brier(predictions: list[tuple[float, int]]) -> None:
    """Update artifact live_brier from (p_hat, actual) pairs; toggle sizing."""
    if len(predictions) < 20:
        return
    model = load_model(force=True)
    if model is None:
        return
    p = np.array([a for a, _ in predictions], dtype=float)
    y = np.array([b for _, b in predictions], dtype=float)
    brier = _brier(p, y)
    model["live_brier"] = round(brier, 4)
    base = float(model.get("brier_val", 0.25))
    model["sizing_enabled"] = brier <= base + BRIER_DEGRADE
    _artifact_path().write_text(json.dumps(model, indent=2), encoding="utf-8")
    global _CACHE
    _CACHE = model


def load_labeled(limit: int = 20000) -> list[dict[str, Any]]:
    path = state_dir() / LABELED_NAME
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    art = train_edge_model()
    print(json.dumps({k: art[k] for k in (
        "n_train", "n_val", "brier_val", "base_rate", "sizing_enabled",
    )} if art else {"ok": False}, indent=2))
