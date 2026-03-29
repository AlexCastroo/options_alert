"""
oi_engine.py — Open Interest calculation engine.

Pure computation module. No I/O, no yfinance, no database access.
Receives pandas DataFrames from OptionsChainSnapshot and returns
frozen dataclasses with calculation results.

Implements:
  1. Max Pain — the strike where total option holder losses are maximized
  2. OI Concentration Map — top strikes ranked by total open interest
  3. GEX (Gamma Exposure) — net dealer gamma to determine market regime
  4. analyze_oi() — single orchestrator entry point for the scheduler

FUTURE EXTENSION: OI buildup detection (day-over-day delta)
FUTURE EXTENSION: Put/Call OI ratio by strike zone (ATM band vs wings)
FUTURE EXTENSION: Volume-weighted OI for intraday flow detection
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

log = logging.getLogger("options_alert.engines.oi_engine")

# ---------------------------------------------------------------------------
# SPX contract multiplier — standard for all CBOE index options
# ---------------------------------------------------------------------------
SPX_CONTRACT_MULTIPLIER: int = 100


# ---------------------------------------------------------------------------
# Result dataclasses — all frozen (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaxPainResult:
    """Result of the Max Pain calculation.

    Attributes:
        strike: The strike price that minimizes total dollar loss for
            option writers (maximizes loss for holders).
        total_pain_at_strike: The total dollar pain at the Max Pain strike.
        spot: The spot price used in the calculation.
        distance_from_spot: Max Pain strike minus spot (signed).
        distance_pct: Distance as percentage of spot.
    """

    strike: float
    total_pain_at_strike: float
    spot: float
    distance_from_spot: float
    distance_pct: float


@dataclass(frozen=True)
class OIConcentration:
    """A single strike's OI concentration entry.

    Attributes:
        strike: The strike price.
        call_oi: Open interest on the call side.
        put_oi: Open interest on the put side.
        total_oi: Combined call + put OI.
        pct_of_total: This strike's share of total OI across all strikes.
    """

    strike: float
    call_oi: int
    put_oi: int
    total_oi: int
    pct_of_total: float


@dataclass(frozen=True)
class GEXResult:
    """Result of the Gamma Exposure calculation.

    # [TRADING IMPLICATION]: assumes standard dealer short-gamma model.
    # Dealers are assumed net short options (short calls from selling to buyers,
    # short puts from selling to buyers). This means:
    #   - Call gamma contributes POSITIVE dealer GEX (dealers hedge by buying dips)
    #   - Put gamma contributes NEGATIVE dealer GEX (dealers hedge by selling dips)
    # When net GEX is positive: dealer hedging dampens moves (range-bound).
    # When net GEX is negative: dealer hedging amplifies moves (trending).

    Attributes:
        net_gex: Net gamma exposure in dollar terms.
        call_gex: Total call-side gamma exposure.
        put_gex: Total put-side gamma exposure (negative).
        regime: "POSITIVE" (range-bound) or "NEGATIVE" (trending).
        flip_strike: The strike where cumulative GEX flips sign, if found.
        spot: The spot price used in the calculation.
    """

    net_gex: float
    call_gex: float
    put_gex: float
    regime: str
    flip_strike: Optional[float]
    spot: float


@dataclass(frozen=True)
class OIAnalysis:
    """Aggregated OI analysis result — single object for the scheduler.

    Attributes:
        max_pain: Max Pain calculation result.
        oi_concentration: Ranked list of top OI concentration strikes.
        gex: Gamma Exposure result.
        expiry_date: The expiry date these calculations apply to.
        symbol: The underlying symbol.
    """

    max_pain: MaxPainResult
    oi_concentration: list[OIConcentration]
    gex: GEXResult
    expiry_date: str
    symbol: str


# ---------------------------------------------------------------------------
# 1. Max Pain
# ---------------------------------------------------------------------------


def calculate_max_pain(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
) -> Optional[MaxPainResult]:
    """Calculate the Max Pain strike for a given options chain.

    Max Pain is the strike price at which the total dollar value of all
    outstanding options (calls + puts) would expire worthless — i.e., the
    point of maximum pain for option holders / minimum payout for writers.

    Algorithm:
        For each candidate strike K, compute:
          call_pain = sum over all call strikes K_i of: max(0, K_i - K) * call_OI_i
          put_pain  = sum over all put strikes K_j of: max(0, K - K_j) * put_OI_j
          total_pain(K) = call_pain + put_pain
        Max Pain = argmin(total_pain(K))

    Args:
        calls: DataFrame with columns [strike, openInterest, ...].
        puts: DataFrame with columns [strike, openInterest, ...].
        spot: Current spot price of the underlying.

    Returns:
        MaxPainResult or None if inputs are insufficient.
    """
    if calls.empty and puts.empty:
        log.warning("Cannot calculate Max Pain: both call and put chains are empty")
        return None

    if spot <= 0:
        log.error("Invalid spot price for Max Pain: %.2f", spot)
        return None

    # Build arrays for vectorized computation
    call_strikes = calls["strike"].values
    call_oi = calls["openInterest"].values
    put_strikes = puts["strike"].values
    put_oi = puts["openInterest"].values

    # Candidate strikes: union of all strikes with OI
    all_strikes = sorted(
        set(call_strikes.tolist() + put_strikes.tolist())
    )

    if not all_strikes:
        log.warning("No strikes available for Max Pain calculation")
        return None

    min_pain = float("inf")
    max_pain_strike = all_strikes[0]

    for k in all_strikes:
        # Call holders lose intrinsic value if underlying settles at K
        # For each call at strike K_i: if K > K_i, call is ITM, holder gains (K - K_i)
        # So writer pays out: max(0, K - K_i) * OI_i ... wait, no.
        #
        # Correctly: if underlying settles at K, a call with strike K_i pays
        # max(0, K - K_i) to the holder. We want the K that minimizes total payouts.
        call_pain = 0.0
        for i in range(len(call_strikes)):
            intrinsic = max(0.0, k - call_strikes[i])
            call_pain += intrinsic * call_oi[i]

        put_pain = 0.0
        for i in range(len(put_strikes)):
            intrinsic = max(0.0, put_strikes[i] - k)
            put_pain += intrinsic * put_oi[i]

        total_pain = call_pain + put_pain

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = k

    distance = max_pain_strike - spot
    distance_pct = (distance / spot) * 100.0 if spot != 0 else 0.0

    log.info(
        "Max Pain: %.0f (spot=%.2f, distance=%.1f pts / %.2f%%)",
        max_pain_strike,
        spot,
        distance,
        distance_pct,
    )

    return MaxPainResult(
        strike=max_pain_strike,
        total_pain_at_strike=min_pain,
        spot=spot,
        distance_from_spot=round(distance, 2),
        distance_pct=round(distance_pct, 4),
    )


# ---------------------------------------------------------------------------
# 2. OI Concentration Map
# ---------------------------------------------------------------------------


def build_oi_concentration_map(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    top_n: int = 15,
) -> list[OIConcentration]:
    """Build a ranked OI concentration map across all strikes.

    Combines call and put OI per strike, ranks by total OI descending,
    and returns the top N strikes. These high-OI strikes act as
    support/resistance walls where dealers have concentrated hedging activity.

    Args:
        calls: DataFrame with columns [strike, openInterest, ...].
        puts: DataFrame with columns [strike, openInterest, ...].
        top_n: Number of top strikes to return. Defaults to 15.

    Returns:
        List of OIConcentration dataclasses, sorted by total_oi descending.
    """
    if calls.empty and puts.empty:
        log.warning("Cannot build OI map: both chains are empty")
        return []

    # Aggregate OI by strike across calls and puts
    call_agg = (
        calls.groupby("strike")["openInterest"]
        .sum()
        .reset_index()
        .rename(columns={"openInterest": "call_oi"})
    )
    put_agg = (
        puts.groupby("strike")["openInterest"]
        .sum()
        .reset_index()
        .rename(columns={"openInterest": "put_oi"})
    )

    merged = pd.merge(call_agg, put_agg, on="strike", how="outer").fillna(0)
    merged["call_oi"] = merged["call_oi"].astype(int)
    merged["put_oi"] = merged["put_oi"].astype(int)
    merged["total_oi"] = merged["call_oi"] + merged["put_oi"]

    # Filter out zero total OI
    merged = merged[merged["total_oi"] > 0]

    if merged.empty:
        log.warning("No strikes with positive OI for concentration map")
        return []

    grand_total = merged["total_oi"].sum()
    merged["pct_of_total"] = (merged["total_oi"] / grand_total * 100.0).round(4)

    # Sort and take top N
    merged = merged.sort_values("total_oi", ascending=False).head(top_n)

    result = [
        OIConcentration(
            strike=float(row["strike"]),
            call_oi=int(row["call_oi"]),
            put_oi=int(row["put_oi"]),
            total_oi=int(row["total_oi"]),
            pct_of_total=float(row["pct_of_total"]),
        )
        for _, row in merged.iterrows()
    ]

    log.info(
        "OI concentration map: top %d strikes, #1 = %.0f (%.1f%% of total OI)",
        len(result),
        result[0].strike if result else 0,
        result[0].pct_of_total if result else 0,
    )

    return result


# ---------------------------------------------------------------------------
# 3. GEX (Gamma Exposure)
# ---------------------------------------------------------------------------


def _bsm_gamma(
    spot: float,
    strike: float,
    iv: float,
    dte_years: float,
) -> float:
    """Approximate BSM gamma for a European option.

    Uses the standard Black-Scholes gamma formula with zero risk-free rate
    and zero dividend yield (acceptable approximation for short-dated weekly
    options where rho contribution is negligible).

    Gamma = N'(d1) / (S * sigma * sqrt(T))
    where d1 = (ln(S/K) + 0.5 * sigma^2 * T) / (sigma * sqrt(T))

    Args:
        spot: Current underlying price.
        strike: Option strike price.
        iv: Implied volatility as a decimal (e.g., 0.20 for 20%).
        dte_years: Time to expiry in years.

    Returns:
        Gamma value, or 0.0 if inputs are degenerate.
    """
    if iv <= 0.0 or dte_years <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return 0.0

    sqrt_t = math.sqrt(dte_years)
    sigma_sqrt_t = iv * sqrt_t

    if sigma_sqrt_t == 0.0:
        return 0.0

    d1 = (math.log(spot / strike) + 0.5 * iv * iv * dte_years) / sigma_sqrt_t

    # N'(d1) = standard normal PDF
    n_prime_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)

    gamma = n_prime_d1 / (spot * sigma_sqrt_t)

    return gamma


def _estimate_dte_years(expiry_date: str) -> float:
    """Estimate days to expiry in years from an expiry date string.

    Uses calendar days with a floor of 1 day to avoid division by zero
    on expiry day itself.

    Args:
        expiry_date: Expiry date in YYYY-MM-DD format.

    Returns:
        DTE in years (trading-day adjusted: /365).
    """
    from datetime import datetime

    try:
        expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        dte_days = max((expiry - today).days, 1)  # Floor at 1
        return dte_days / 365.0
    except ValueError:
        log.warning("Could not parse expiry date '%s' — defaulting to 1 day", expiry_date)
        return 1.0 / 365.0


def calculate_gex(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    expiry_date: str = "",
) -> Optional[GEXResult]:
    """Calculate net Gamma Exposure (GEX) for the options chain.

    # [TRADING IMPLICATION]: assumes standard dealer short-gamma model.
    # Dealers are assumed to be net short both calls and puts (retail/inst buy,
    # dealers sell). Under this model:
    #   - Calls: dealers are short calls → they have NEGATIVE gamma on their book
    #     → to hedge, they BUY when price rises, SELL when it drops
    #     → this DAMPENS moves → positive GEX contribution
    #   - Puts: dealers are short puts → they have POSITIVE gamma on their book
    #     (short put = negative gamma, but the sign convention for GEX means
    #     put gamma contribution is subtracted)
    #     → to hedge, they SELL when price drops, BUY when it rises
    #     → this AMPLIFIES moves → negative GEX contribution
    #
    # Net positive GEX → range-bound (dealer hedging acts as buffer)
    # Net negative GEX → trending (dealer hedging amplifies directional moves)

    Args:
        calls: DataFrame with columns [strike, openInterest, impliedVolatility, ...].
        puts: DataFrame with columns [strike, openInterest, impliedVolatility, ...].
        spot: Current spot price.
        expiry_date: Expiry date string for DTE calculation. If empty, defaults
            to 1 day (conservative for weeklies near expiry).

    Returns:
        GEXResult or None if inputs are insufficient.
    """
    if calls.empty and puts.empty:
        log.warning("Cannot calculate GEX: both chains are empty")
        return None

    if spot <= 0:
        log.error("Invalid spot price for GEX: %.2f", spot)
        return None

    dte_years = _estimate_dte_years(expiry_date) if expiry_date else (1.0 / 365.0)

    # --- Call GEX (positive contribution) ---
    total_call_gex = 0.0
    for _, row in calls.iterrows():
        strike = float(row["strike"])
        oi = int(row["openInterest"])
        iv = float(row["impliedVolatility"])

        if oi <= 0 or iv <= 0:
            continue

        gamma = _bsm_gamma(spot, strike, iv, dte_years)
        # GEX per contract = gamma * OI * spot * multiplier
        # Spot factor: gamma is per $1 move; multiply by spot to get dollar gamma
        contract_gex = gamma * oi * spot * SPX_CONTRACT_MULTIPLIER
        total_call_gex += contract_gex

    # --- Put GEX (negative contribution — dealers short puts amplify moves) ---
    total_put_gex = 0.0
    for _, row in puts.iterrows():
        strike = float(row["strike"])
        oi = int(row["openInterest"])
        iv = float(row["impliedVolatility"])

        if oi <= 0 or iv <= 0:
            continue

        gamma = _bsm_gamma(spot, strike, iv, dte_years)
        contract_gex = gamma * oi * spot * SPX_CONTRACT_MULTIPLIER
        total_put_gex += contract_gex

    # Net GEX: calls positive, puts negative
    net_gex = total_call_gex - total_put_gex
    regime = "POSITIVE" if net_gex >= 0 else "NEGATIVE"

    # --- Flip strike: where cumulative GEX crosses zero ---
    flip_strike = _find_gex_flip_strike(calls, puts, spot, dte_years)

    log.info(
        "GEX: net=%.0f | calls=%.0f puts=%.0f | regime=%s | flip=%.0f",
        net_gex,
        total_call_gex,
        total_put_gex,
        regime,
        flip_strike if flip_strike is not None else 0,
    )

    return GEXResult(
        net_gex=round(net_gex, 2),
        call_gex=round(total_call_gex, 2),
        put_gex=round(-total_put_gex, 2),  # Store as negative for clarity
        regime=regime,
        flip_strike=flip_strike,
        spot=spot,
    )


def _find_gex_flip_strike(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    dte_years: float,
) -> Optional[float]:
    """Find the strike where cumulative GEX flips from positive to negative.

    Walks strikes from low to high, accumulating per-strike net GEX.
    The flip point is where dealers transition from dampening to amplifying.

    Args:
        calls: Normalized calls DataFrame.
        puts: Normalized puts DataFrame.
        spot: Current spot price.
        dte_years: Time to expiry in years.

    Returns:
        The flip strike, or None if GEX is uniformly signed.
    """
    # Build per-strike GEX
    call_gex_map: dict[float, float] = {}
    for _, row in calls.iterrows():
        strike = float(row["strike"])
        oi = int(row["openInterest"])
        iv = float(row["impliedVolatility"])
        if oi <= 0 or iv <= 0:
            continue
        gamma = _bsm_gamma(spot, strike, iv, dte_years)
        call_gex_map[strike] = call_gex_map.get(strike, 0.0) + (
            gamma * oi * spot * SPX_CONTRACT_MULTIPLIER
        )

    put_gex_map: dict[float, float] = {}
    for _, row in puts.iterrows():
        strike = float(row["strike"])
        oi = int(row["openInterest"])
        iv = float(row["impliedVolatility"])
        if oi <= 0 or iv <= 0:
            continue
        gamma = _bsm_gamma(spot, strike, iv, dte_years)
        put_gex_map[strike] = put_gex_map.get(strike, 0.0) + (
            gamma * oi * spot * SPX_CONTRACT_MULTIPLIER
        )

    all_strikes = sorted(set(list(call_gex_map.keys()) + list(put_gex_map.keys())))
    if not all_strikes:
        return None

    # Per-strike net GEX: call contribution (positive) minus put contribution (negative)
    prev_net = None
    for strike in all_strikes:
        call_g = call_gex_map.get(strike, 0.0)
        put_g = put_gex_map.get(strike, 0.0)
        net = call_g - put_g

        if prev_net is not None:
            # Detect sign change
            if (prev_net >= 0 and net < 0) or (prev_net < 0 and net >= 0):
                return strike

        prev_net = net

    return None


# ---------------------------------------------------------------------------
# 4. Orchestrator
# ---------------------------------------------------------------------------


def analyze_oi(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    spot: float,
    expiry_date: str = "",
    symbol: str = "^SPX",
    top_n: int = 15,
) -> Optional[OIAnalysis]:
    """Run the full OI analysis suite — single entry point for the scheduler.

    Calls all three calculation functions and packages results into a
    single OIAnalysis object. Returns None if any critical calculation fails.

    Args:
        calls: Normalized calls DataFrame from OptionsChainSnapshot.
        puts: Normalized puts DataFrame from OptionsChainSnapshot.
        spot: Current spot price.
        expiry_date: Expiry date string in YYYY-MM-DD format.
        symbol: Underlying symbol for labeling.
        top_n: Number of top strikes for OI concentration map.

    Returns:
        OIAnalysis or None if critical calculations fail.
    """
    if calls.empty and puts.empty:
        log.error("Cannot analyze OI: no data in either chain")
        return None

    if spot <= 0:
        log.error("Cannot analyze OI: invalid spot price %.2f", spot)
        return None

    log.info(
        "Running OI analysis: %s expiry=%s spot=%.2f (%d calls, %d puts)",
        symbol,
        expiry_date,
        spot,
        len(calls),
        len(puts),
    )

    # Max Pain
    max_pain = calculate_max_pain(calls, puts, spot)
    if max_pain is None:
        log.error("Max Pain calculation failed — aborting OI analysis")
        return None

    # OI Concentration
    oi_concentration = build_oi_concentration_map(calls, puts, top_n=top_n)
    if not oi_concentration:
        log.warning("OI concentration map is empty — proceeding with empty list")

    # GEX
    gex = calculate_gex(calls, puts, spot, expiry_date=expiry_date)
    if gex is None:
        log.error("GEX calculation failed — aborting OI analysis")
        return None

    result = OIAnalysis(
        max_pain=max_pain,
        oi_concentration=oi_concentration,
        gex=gex,
        expiry_date=expiry_date,
        symbol=symbol,
    )

    log.info(
        "OI analysis complete: MaxPain=%.0f | GEX regime=%s | Top OI strike=%.0f",
        max_pain.strike,
        gex.regime,
        oi_concentration[0].strike if oi_concentration else 0,
    )

    return result
