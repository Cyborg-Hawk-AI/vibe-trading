# Factor Investing

**The systematic harvesting of documented return premia.** The academic
backbone of quant equity — from CAPM (1964) → Fama-French 3-factor (1992) →
the modern factor zoo. Slow (monthly+ horizons) but the evidence base is the
deepest in finance, and factors make superb *filters* for faster strategies.

## The factors that survived replication

| Factor | Definition | Long-run premium | Best theory |
|---|---|---|---|
| **Value** | Cheap vs expensive (B/P, E/P, FCF yield) | ~3–5%/yr L/S | Risk + overextrapolation of bad news |
| **Momentum** | 12-1 month winners vs losers | ~6–8%/yr L/S | Underreaction, herding |
| **Quality/Profitability** | High ROE/gross profit, stable earnings, low accruals | ~3–4%/yr | Investors underprice boring compounders |
| **Low volatility** | Low-beta/low-vol stocks beat high-vol | ~2–4%/yr risk-adj | Lottery preference + leverage constraints |
| **Size** | Small beats large | weak alone, strong *within* quality | Liquidity premium |
| **Carry** | High-yield assets vs low (FX, bonds, commodities) | asset-class dependent | Risk premium for crash exposure |

Key modern findings:
- **Factors combine multiplicatively well:** value+momentum is the classic
  pair (negatively correlated premia — Asness's AQR core). Quality+size
  rescues the size effect ("size matters, if you control your junk").
- Half the published factor zoo fails replication (Hou-Xue-Zhang); stick to
  the big five above.
- Factor premia have **decade-long droughts** (value 2010–2020). Sizing and
  patience matter more than discovery.

## Buffett decoded
AQR's *Buffett's Alpha*: Berkshire's record ≈ long cheap, safe, quality
stocks + ~1.6× cheap insurance-float leverage + decades of discipline. The
most famous discretionary record is substantially a factor portfolio.

## Uses for a shorter-horizon system
Factors are too slow to *be* the strategy at a 2-week horizon, but they are
excellent **universe shapers**:
- Trade breakouts/momentum only within high-quality, high-RS names
  (O'Neil's approach is essentially quality+momentum+breakout timing).
- Avoid the bottom decile of quality — where bankruptcies, frauds, and
  dilution-machines live (most penny stocks sit here; see `crypto-specific.md`
  and penny-stock cautions).
- Low-vol names mean-revert more reliably; high-vol junk trends violently
  both directions.

## Implementation checklist
- Rebalance monthly; trade patiently (factor alpha dies from costs at high turnover).
- Diversify across factors — never all-in on one premium.
- Use factor scores as features/filters in faster models rather than
  standalone signals.
