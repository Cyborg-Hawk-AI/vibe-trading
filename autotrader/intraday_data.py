"""Intraday (5m/15m) snapshot library for the daytrade replay engine.

Three feeders into ``autotrader/state/data/daytrade__{5m,15m}/``:

  1. ``backfill_equities`` — yfinance intraday bars (Yahoo serves ~60 days
     of 5m/15m history for free). Timestamps are stored tz-naive in
     America/New_York so one calendar date == one RTH session.
  2. ``backfill_crypto`` — OKX public candles through the repo's loader
     (paginated, no key). Timestamps are tz-naive UTC; a session is a UTC day.
  3. ``ingest_live_tape`` — livetrader's recorded 1m session tapes
     (``livetrader/state/tape/YYYY-MM-DD/*.parquet``) resampled to 5m/15m
     and merged in. This is how the library outgrows Yahoo's 60-day window:
     every live trading day adds a session forever.

All writes are merge+dedupe, so re-runs and overlapping fetches are safe.

    python -m autotrader.intraday_data            # full backfill + tape ingest
    python -m autotrader.intraday_data --tape-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

from autotrader.bootstrap import ensure_agent_path
from autotrader.data import snapshot_dir, snapshot_for_interval
from autotrader.universe import CRYPTO_TOP, PENNY_LIQUID, US_LIQUID

ensure_agent_path()

logger = logging.getLogger("autotrader.intraday")

SNAPSHOT_BASE = "daytrade"
INTRADAY_INTERVALS = ("5m", "15m")
_INTERVAL_MINUTES = {"5m": 5, "15m": 15}
_OHLCV_AGG = {"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"}

TAPE_ROOT = Path(__file__).resolve().parents[1] / "livetrader" / "state" / "tape"


def _yf_symbol(code: str) -> str:
    return code[:-3] if code.endswith(".US") else code


def _merge_save(snap: Path, code: str, frame: pd.DataFrame) -> int:
    """Merge new bars into the persisted frame (dedupe by timestamp)."""
    if frame is None or frame.empty:
        return 0
    path = snap / f"{code.replace('/', '_')}.parquet"
    if path.exists():
        try:
            old = pd.read_parquet(path)
            frame = pd.concat([old, frame])
        except Exception:  # noqa: BLE001 — a corrupt frame gets rebuilt
            logger.warning("unreadable frame %s; rewriting", path.name)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.to_parquet(path)
    return len(frame)


def _write_manifest(snap: Path, interval: str) -> None:
    manifest = {
        "interval": interval,
        "updated": f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}",
        "symbols": sorted(p.stem for p in snap.glob("*.parquet")
                          if not p.name.startswith("._")),
    }
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Equities via yfinance (free intraday: ~60 days of 5m/15m)
# ---------------------------------------------------------------------------

def backfill_equities(
    codes: list[str] | None = None,
    intervals: tuple[str, ...] = INTRADAY_INTERVALS,
    *,
    period: str = "60d",
    chunk: int = 40,
) -> dict[str, int]:
    import yfinance as yf

    codes = codes or (US_LIQUID + PENNY_LIQUID)
    summary: dict[str, int] = {}
    for interval in intervals:
        snap = snapshot_dir(snapshot_for_interval(SNAPSHOT_BASE, interval))
        saved = 0
        for i in range(0, len(codes), chunk):
            batch = codes[i:i + chunk]
            tickers = [_yf_symbol(c) for c in batch]
            try:
                df = yf.download(
                    tickers=" ".join(tickers), period=period, interval=interval,
                    group_by="ticker", threads=True, progress=False,
                    prepost=False, auto_adjust=False,
                )
            except Exception as exc:  # noqa: BLE001 — one bad chunk must not stop the rest
                logger.warning("yfinance chunk %s failed: %s", tickers[:3], exc)
                continue
            for code, ticker in zip(batch, tickers):
                try:
                    sub = df[ticker] if isinstance(df.columns, pd.MultiIndex) else df
                except KeyError:
                    continue
                sub = sub.rename(columns=str.lower)
                keep = [c for c in ("open", "high", "low", "close", "volume")
                        if c in sub.columns]
                sub = sub[keep].dropna(subset=["close"])
                if sub.empty:
                    continue
                idx = pd.DatetimeIndex(sub.index)
                if idx.tz is not None:
                    idx = idx.tz_convert("America/New_York").tz_localize(None)
                sub = sub.copy()
                sub.index = idx
                if _merge_save(snap, code, sub):
                    saved += 1
            time.sleep(1.0)  # be gentle with Yahoo
        _write_manifest(snap, interval)
        summary[interval] = saved
        logger.info("equities %s: %d/%d symbols persisted", interval, saved, len(codes))
    return summary


# ---------------------------------------------------------------------------
# Crypto via OKX (public candles, paginated)
# ---------------------------------------------------------------------------

_OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"


def _okx_history(inst_id: str, start_ts: int, end_ts: int, bar: str) -> pd.DataFrame | None:
    """Deep paginated fetch via the history-candles endpoint.

    The regular ``market/candles`` endpoint only serves the most recent
    ~1,440 candles regardless of pagination; 60 days of 5m bars needs
    ``market/history-candles`` (100 rows/page, paginate backward with
    ``after``).
    """
    import requests

    rows: list = []
    after = str(end_ts)
    for _ in range(400):  # 400 * 100 rows covers 60d of 5m with margin
        try:
            resp = requests.get(
                _OKX_HISTORY_URL,
                params={"instId": inst_id, "bar": bar, "limit": "100", "after": after},
                timeout=15,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("okx history %s page failed: %s", inst_id, exc)
            break
        if data.get("code") != "0" or not data.get("data"):
            break
        page = data["data"]
        rows.extend(page)
        oldest = int(page[-1][0])
        if oldest <= start_ts or len(page) < 100:
            break
        after = str(oldest)
        time.sleep(0.12)  # OKX rate limit: 20 req / 2s on this endpoint
    if not rows:
        return None
    cols = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = pd.DataFrame(rows, columns=cols)
    df = df[df["confirm"] == "1"]
    df["trade_date"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
    df = df.set_index("trade_date").sort_index()
    df = df[(df.index >= pd.Timestamp(start_ts, unit="ms"))
            & (df.index < pd.Timestamp(end_ts, unit="ms"))]
    df = df[["open", "high", "low", "close", "volume"]].dropna(
        subset=["open", "high", "low", "close"]
    )
    return df if not df.empty else None


def backfill_crypto(
    codes: list[str] | None = None,
    intervals: tuple[str, ...] = INTRADAY_INTERVALS,
    *,
    days: int = 60,
) -> dict[str, int]:
    codes = codes or list(CRYPTO_TOP)
    end = pd.Timestamp.utcnow().tz_localize(None)
    start = end - pd.Timedelta(days=days)
    start_ts = int(start.timestamp() * 1000)
    end_ts = int(end.timestamp() * 1000)
    summary: dict[str, int] = {}
    for interval in intervals:
        snap = snapshot_dir(snapshot_for_interval(SNAPSHOT_BASE, interval))
        saved = 0
        for code in codes:
            frame = _okx_history(code, start_ts, end_ts, interval)
            if frame is not None and _merge_save(snap, code, frame):
                saved += 1
        _write_manifest(snap, interval)
        summary[interval] = saved
        logger.info("crypto %s: %d/%d pairs persisted", interval, saved, len(codes))
    return summary


# ---------------------------------------------------------------------------
# Livetrader session tape ingest (1m parquet -> 5m/15m snapshots)
# ---------------------------------------------------------------------------

def _tape_code(symbol: str) -> str | None:
    """Map a livetrader tape symbol to a snapshot code (None = skip)."""
    if symbol.endswith("-USDT"):
        return symbol
    if symbol.isalpha() and symbol.isupper():
        return f"{symbol}.US"
    return None


def ingest_live_tape(
    tape_root: Path | None = None,
    intervals: tuple[str, ...] = INTRADAY_INTERVALS,
) -> int:
    """Resample recorded 1m session tapes into the daytrade snapshots.

    Processed date directories get a ``.ingested`` marker so ingest stays
    idempotent and cheap to call at the top of every daytrade cycle.
    """
    root = tape_root or TAPE_ROOT
    if not root.exists():
        return 0
    ingested = 0
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        marker = day_dir / ".ingested"
        if marker.exists():
            continue
        for path in sorted(day_dir.glob("*.parquet")):
            if path.name.startswith("._"):
                continue
            code = _tape_code(path.stem)
            if code is None:
                continue
            try:
                bars = pd.read_parquet(path)
            except Exception:  # noqa: BLE001
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            idx = pd.DatetimeIndex(bars.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            # Equities sessions live in ET; crypto sessions in UTC.
            tz = "UTC" if code.endswith("-USDT") else "America/New_York"
            bars = bars.copy()
            bars.index = idx.tz_convert(tz).tz_localize(None)
            for interval in intervals:
                snap = snapshot_dir(snapshot_for_interval(SNAPSHOT_BASE, interval))
                res = (
                    bars.resample(f"{_INTERVAL_MINUTES[interval]}min")
                    .agg(_OHLCV_AGG)
                    .dropna(subset=["close"])
                )
                _merge_save(snap, code, res)
            ingested += 1
        marker.write_text(f"{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
    if ingested:
        for interval in intervals:
            _write_manifest(
                snapshot_dir(snapshot_for_interval(SNAPSHOT_BASE, interval)), interval
            )
        logger.info("tape ingest: %d symbol-sessions merged", ingested)
    return ingested


def refresh_recent(days: int = 5) -> None:
    """Cheap incremental refresh: last few days of bars + any new tape."""
    backfill_equities(period=f"{days}d")
    backfill_crypto(days=days)
    ingest_live_tape()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the intraday daytrade snapshot library")
    parser.add_argument("--days", type=int, default=60, help="crypto lookback days")
    parser.add_argument("--period", default="60d", help="yfinance period (max 60d)")
    parser.add_argument("--tape-only", action="store_true")
    parser.add_argument("--skip-crypto", action="store_true")
    parser.add_argument("--skip-equities", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.tape_only:
        if not args.skip_equities:
            print("equities:", backfill_equities(period=args.period))
        if not args.skip_crypto:
            print("crypto:", backfill_crypto(days=args.days))
    print("tape sessions ingested:", ingest_live_tape())
    return 0


if __name__ == "__main__":
    sys.exit(main())
