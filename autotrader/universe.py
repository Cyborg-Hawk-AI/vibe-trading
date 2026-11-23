"""Ticker universes for the simulation sweeps.

Symbols use vibe-trading loader formats: ``AAPL.US`` for US equities (routes
to the us_equity fallback chain: yahoo -> stooq -> sina -> ... -> yfinance)
and ``BTC-USDT`` for crypto (okx -> ccxt -> yfinance).

Lists are static and curated for liquidity — the point-in-time caveat is that
today's liquid names carry survivorship bias over the lookback window; this is
documented in ARCHITECTURE.md and acceptable for strategy-shape search.
"""

from __future__ import annotations

# ~120 of the most liquid US large/mega caps + high-volume ETFs, roughly by
# average dollar volume. Slice with ``equity_universe_size``.
US_LIQUID: list[str] = [
    # Mega-cap tech
    "AAPL.US", "MSFT.US", "NVDA.US", "GOOGL.US", "AMZN.US", "META.US",
    "TSLA.US", "AVGO.US", "AMD.US", "NFLX.US", "CRM.US", "ORCL.US",
    "ADBE.US", "INTC.US", "QCOM.US", "TXN.US", "MU.US", "AMAT.US",
    "LRCX.US", "KLAC.US", "PLTR.US", "SNOW.US", "UBER.US", "SHOP.US",
    # Financials
    "JPM.US", "BAC.US", "WFC.US", "GS.US", "MS.US", "C.US",
    "V.US", "MA.US", "AXP.US", "BLK.US", "SCHW.US", "COIN.US",
    # Healthcare
    "LLY.US", "UNH.US", "JNJ.US", "ABBV.US", "MRK.US", "PFE.US",
    "TMO.US", "ABT.US", "AMGN.US", "GILD.US", "ISRG.US", "CVS.US",
    # Consumer
    "WMT.US", "COST.US", "HD.US", "LOW.US", "MCD.US", "SBUX.US",
    "NKE.US", "TGT.US", "DIS.US", "PG.US", "KO.US", "PEP.US",
    "PM.US", "MDLZ.US", "BKNG.US", "ABNB.US", "MAR.US", "CMG.US",
    # Industrials / energy
    "CAT.US", "DE.US", "BA.US", "GE.US", "HON.US", "UNP.US",
    "UPS.US", "FDX.US", "LMT.US", "RTX.US", "XOM.US", "CVX.US",
    "COP.US", "SLB.US", "OXY.US", "EOG.US", "FCX.US", "NEM.US",
    # Comms / media / other
    "T.US", "VZ.US", "TMUS.US", "CMCSA.US", "IBM.US", "NOW.US",
    "INTU.US", "PYPL.US", "SQ.US", "SOFI.US", "F.US", "GM.US",
    "RIVN.US", "DAL.US", "UAL.US", "AAL.US", "CCL.US", "NCLH.US",
    # High-volume ETFs (liquid trading vehicles)
    "SPY.US", "QQQ.US", "IWM.US", "DIA.US", "XLF.US", "XLE.US",
    "XLK.US", "SMH.US", "ARKK.US", "GDX.US", "TLT.US", "HYG.US",
    "EEM.US", "FXI.US", "EFA.US", "GLD.US", "SLV.US", "USO.US",
    "XBI.US", "KRE.US", "XLU.US", "XLV.US", "XLI.US", "XLP.US",
]

# Top USDT perp/spot pairs by volume on OKX-style venues.
CRYPTO_TOP: list[str] = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT",
    "DOT-USDT", "TON-USDT", "SUI-USDT", "APT-USDT", "ARB-USDT",
    "OP-USDT", "NEAR-USDT", "ATOM-USDT", "FIL-USDT", "UNI-USDT",
    "AAVE-USDT", "INJ-USDT", "PEPE-USDT", "SHIB-USDT", "WLD-USDT",
    "TRX-USDT", "ETC-USDT", "BCH-USDT", "HBAR-USDT", "RENDER-USDT",
]

# High-volume, low-priced US names ("penny-ish": typically < $10, many < $5).
# Curated for liquidity — these trade tens of millions of shares/day, so the
# 0.05% slippage model stays defensible. True sub-$1 OTC pinks are excluded:
# their spreads/fills cannot be simulated honestly with free daily data.
PENNY_LIQUID: list[str] = [
    "SNDL.US", "PLUG.US", "NOK.US", "SIRI.US", "LCID.US",
    "NIO.US", "AMC.US", "BB.US", "PLTR.US", "OPEN.US",
    "GRAB.US", "IONQ.US", "RIG.US", "BTG.US", "TLRY.US",
    "CGC.US", "ACB.US", "HEXO.US", "VALE.US", "ITUB.US",
    "BBD.US", "ABEV.US", "KGC.US", "GOLD.US", "UEC.US",
    "DNN.US", "FCEL.US", "BLDP.US", "CHPT.US", "GOEV.US",
    "MARA.US", "RIOT.US", "BITF.US", "HUT.US", "CLSK.US",
    "SOUN.US", "BBAI.US", "RKLB.US", "ASTS.US", "SPCE.US",
]

_UNIVERSES: dict[str, list[str]] = {
    "us_liquid": US_LIQUID,
    "crypto_top": CRYPTO_TOP,
    "penny_liquid": PENNY_LIQUID,
}


def get_universe(name: str, size: int | None = None) -> list[str]:
    """Return a universe by name, optionally truncated to ``size`` symbols."""
    try:
        symbols = _UNIVERSES[name]
    except KeyError:
        raise ValueError(
            f"Unknown universe {name!r}; available: {sorted(_UNIVERSES)}"
        ) from None
    if size is not None and size == 0:
        return []
    if size is not None and size > 0:
        return symbols[:size]
    return list(symbols)
