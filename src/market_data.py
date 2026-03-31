"""
market_data.py — Data ingestion layer for the Options Alert System.

This module is the ONLY place that touches external data sources for options data.
Everything downstream receives clean dataclasses — no yfinance objects leak out.

Current data sources:
  - yfinance: SPX options chain (polling, with requests-cache)

FUTURE EXTENSION: Polygon.io WebSocket feed for real-time OI updates
FUTURE EXTENSION: IBEX 35 options chain via different provider
"""

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests as http_requests
import yfinance as yf

log = logging.getLogger("options_alert.market_data")

# ---------------------------------------------------------------------------
# In-memory TTL cache for OI fetches — 5-minute TTL.
# OI is exchange-reported once daily; intraday changes are negligible.
# The cache prevents hammering yfinance on every 60s polling cycle.
#
# Note: requests-cache is incompatible with yfinance's curl_cffi backend.
# We use a simple time-based in-memory cache instead.
# ---------------------------------------------------------------------------
_oi_cache: dict[str, tuple[datetime, "OptionsChainSnapshot"]] = {}
_oi_cache_lock = threading.Lock()
_OI_CACHE_TTL_SECONDS: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Fixed column contract for downstream consumers
# ---------------------------------------------------------------------------
OI_CHAIN_COLUMNS = ["strike", "openInterest", "volume", "impliedVolatility"]


@dataclass(frozen=True)
class OptionsChainSnapshot:
    """Immutable snapshot of a single expiry's options chain.

    Attributes:
        symbol: Underlying ticker symbol (e.g. "^SPX").
        expiry_date: The expiry date string in YYYY-MM-DD format.
        spot: Spot price at the time of the fetch.
        calls: DataFrame with columns per OI_CHAIN_COLUMNS contract.
        puts: DataFrame with columns per OI_CHAIN_COLUMNS contract.
        fetch_timestamp: UTC timestamp of when the data was fetched.
    """

    symbol: str
    expiry_date: str
    spot: float
    calls: pd.DataFrame = field(repr=False)
    puts: pd.DataFrame = field(repr=False)
    fetch_timestamp: datetime = field(default_factory=datetime.utcnow)


def _find_nearest_friday_expiry(available_expiries: list[str]) -> Optional[str]:
    """Select the nearest Friday expiry from a list of date strings.

    Handles the holiday edge case: if the nearest Friday is missing from
    the available list (market holiday), falls back to the nearest Thursday.

    Args:
        available_expiries: List of expiry date strings from yfinance
            in YYYY-MM-DD format.

    Returns:
        The selected expiry date string, or None if no valid expiry found.
    """
    if not available_expiries:
        log.warning("No expiries available for selection")
        return None

    today = datetime.utcnow().date()
    expiry_dates = []

    for exp_str in available_expiries:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            expiry_dates.append((exp_str, exp_date))
        except ValueError:
            log.debug("Skipping unparseable expiry string: %s", exp_str)
            continue

    if not expiry_dates:
        log.warning("No parseable expiry dates found")
        return None

    # Sort by date ascending
    expiry_dates.sort(key=lambda x: x[1])

    # First pass: find the nearest Friday expiry that is >= today
    for exp_str, exp_date in expiry_dates:
        if exp_date >= today and exp_date.weekday() == 4:  # 4 = Friday
            log.info("Selected Friday expiry: %s", exp_str)
            return exp_str

    # Second pass (holiday fallback): nearest Thursday >= today
    for exp_str, exp_date in expiry_dates:
        if exp_date >= today and exp_date.weekday() == 3:  # 3 = Thursday
            log.info(
                "No Friday expiry found — holiday fallback to Thursday: %s",
                exp_str,
            )
            return exp_str

    # Third pass: nearest Wednesday >= today (double holiday edge case)
    for exp_str, exp_date in expiry_dates:
        if exp_date >= today and exp_date.weekday() == 2:  # 2 = Wednesday
            log.info(
                "No Friday/Thursday expiry — fallback to Wednesday: %s",
                exp_str,
            )
            return exp_str

    # Last resort: take the nearest future expiry regardless of day
    for exp_str, exp_date in expiry_dates:
        if exp_date >= today:
            log.warning(
                "No standard weekly expiry found — using nearest available: %s",
                exp_str,
            )
            return exp_str

    log.error("All available expiries are in the past")
    return None


def _normalize_chain(raw_chain: pd.DataFrame) -> pd.DataFrame:
    """Normalize a raw yfinance options chain to our fixed column contract.

    Handles yfinance inconsistencies:
      - openInterest can be int64 or float64 depending on the fetch
      - Missing columns get zero-filled
      - NaN values in OI/volume are filled with 0

    Args:
        raw_chain: Raw DataFrame from yfinance option_chain().calls or .puts.

    Returns:
        Clean DataFrame with exactly OI_CHAIN_COLUMNS columns.
    """
    if raw_chain is None or raw_chain.empty:
        return pd.DataFrame(columns=OI_CHAIN_COLUMNS)

    df = raw_chain.copy()

    # Ensure all required columns exist
    for col in OI_CHAIN_COLUMNS:
        if col not in df.columns:
            log.warning("Missing column '%s' in chain — zero-filling", col)
            df[col] = 0

    # Select only our contract columns
    df = df[OI_CHAIN_COLUMNS].copy()

    # Fill NaN before casting
    df["openInterest"] = df["openInterest"].fillna(0).astype(int)
    df["volume"] = df["volume"].fillna(0).astype(int)
    df["impliedVolatility"] = df["impliedVolatility"].fillna(0.0).astype(float)
    df["strike"] = df["strike"].astype(float)

    # Reset index for clean downstream usage
    df = df.reset_index(drop=True)

    return df


def _filter_zero_oi_strikes(
    calls: pd.DataFrame, puts: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Remove strikes that have zero OI on BOTH sides.

    Strikes with OI on only one side (e.g. deep OTM puts) are kept —
    they carry signal for Max Pain and GEX calculations.

    Args:
        calls: Normalized calls DataFrame.
        puts: Normalized puts DataFrame.

    Returns:
        Tuple of (filtered_calls, filtered_puts).
    """
    if calls.empty and puts.empty:
        return calls, puts

    # Merge on strike to find combined OI
    merged = pd.merge(
        calls[["strike", "openInterest"]],
        puts[["strike", "openInterest"]],
        on="strike",
        how="outer",
        suffixes=("_call", "_put"),
    ).fillna(0)

    merged["total_oi"] = merged["openInterest_call"] + merged["openInterest_put"]
    active_strikes = set(merged.loc[merged["total_oi"] > 0, "strike"].values)

    if not active_strikes:
        log.warning("All strikes have zero OI on both sides")
        return calls.iloc[0:0], puts.iloc[0:0]

    filtered_calls = calls[calls["strike"].isin(active_strikes)].reset_index(drop=True)
    filtered_puts = puts[puts["strike"].isin(active_strikes)].reset_index(drop=True)

    removed_count = len(calls) + len(puts) - len(filtered_calls) - len(filtered_puts)
    if removed_count > 0:
        log.debug(
            "Filtered %d rows with zero OI on both sides (kept %d strikes)",
            removed_count,
            len(active_strikes),
        )

    return filtered_calls, filtered_puts


def fetch_options_chain(
    symbol: str = "^SPX",
    spot: Optional[float] = None,
) -> Optional[OptionsChainSnapshot]:
    """Fetch the nearest weekly expiry options chain for a given symbol.

    This is the single entry point for all OI data ingestion. Uses a
    5-minute request cache to avoid redundant fetches within a polling cycle.

    Args:
        symbol: Ticker symbol to fetch. Defaults to "^SPX".
        spot: Current spot price. If None, will be fetched from the ticker.

    Returns:
        OptionsChainSnapshot if successful, None on failure.
    """
    # Check in-memory cache first
    cache_key = f"{symbol}"
    with _oi_cache_lock:
        if cache_key in _oi_cache:
            cached_time, cached_snapshot = _oi_cache[cache_key]
            age_seconds = (datetime.utcnow() - cached_time).total_seconds()
            if age_seconds < _OI_CACHE_TTL_SECONDS:
                log.debug(
                    "OI cache hit for %s (age=%.0fs, TTL=%ds)",
                    symbol,
                    age_seconds,
                    _OI_CACHE_TTL_SECONDS,
                )
                return cached_snapshot
            else:
                log.debug("OI cache expired for %s (age=%.0fs)", symbol, age_seconds)

    log.info("Fetching options chain for %s", symbol)

    try:
        ticker = yf.Ticker(symbol)

        # Get available expiries
        available_expiries = list(ticker.options)
        if not available_expiries:
            log.error("No options expiries available for %s", symbol)
            return None

        log.debug(
            "Available expiries for %s: %s",
            symbol,
            available_expiries[:10],
        )

        # Select nearest Friday (or holiday fallback)
        selected_expiry = _find_nearest_friday_expiry(available_expiries)
        if selected_expiry is None:
            log.error("Could not determine weekly expiry for %s", symbol)
            return None

        # Fetch spot if not provided
        if spot is None:
            hist = ticker.history(period="1d")
            if hist.empty:
                log.error("Could not fetch spot price for %s", symbol)
                return None
            spot = float(hist["Close"].iloc[-1])
            log.info("Fetched spot for %s: %.2f", symbol, spot)

        # Fetch the chain for selected expiry
        chain = ticker.option_chain(selected_expiry)

        raw_calls = chain.calls
        raw_puts = chain.puts

        if raw_calls.empty and raw_puts.empty:
            log.error(
                "Empty options chain for %s expiry %s", symbol, selected_expiry
            )
            return None

        # Normalize to fixed column contract
        calls = _normalize_chain(raw_calls)
        puts = _normalize_chain(raw_puts)

        # Filter strikes with zero OI on both sides
        calls, puts = _filter_zero_oi_strikes(calls, puts)

        log.info(
            "Options chain ready: %s expiry=%s | %d call strikes, %d put strikes | spot=%.2f",
            symbol,
            selected_expiry,
            len(calls),
            len(puts),
            spot,
        )

        snapshot = OptionsChainSnapshot(
            symbol=symbol,
            expiry_date=selected_expiry,
            spot=spot,
            calls=calls,
            puts=puts,
        )

        # Store in cache
        with _oi_cache_lock:
            _oi_cache[cache_key] = (datetime.utcnow(), snapshot)

        return snapshot

    except Exception:
        log.exception("Failed to fetch options chain for %s", symbol)
        return None


# ---------------------------------------------------------------------------
# Equity all-expiries OI cache — 30-minute TTL
# OI is exchange-reported once daily; 30-min refresh is more than enough.
# ---------------------------------------------------------------------------
_equity_all_expiries_cache: dict[str, tuple[datetime, list["OptionsChainSnapshot"]]] = {}
_equity_all_expiries_cache_lock = threading.Lock()
_EQUITY_OI_CACHE_TTL_SECONDS: int = 1800  # 30 minutes


def fetch_all_expiries_chain(
    symbol: str,
    spot: Optional[float] = None,
) -> list[OptionsChainSnapshot]:
    """Fetch the full options chain across ALL available expiries for an equity.

    Used for unusual-OI scanning where we need to look across every expiry
    date, not just the nearest weekly. Each expiry returns one
    OptionsChainSnapshot with normalized calls/puts DataFrames.

    Uses a 30-minute cache — OI is exchange-reported once daily, so
    intraday refreshes beyond 30 minutes add no incremental signal.

    Args:
        symbol: Equity ticker (e.g. "PYPL"). Not for index symbols (^SPX).
        spot: Current spot price. If None, fetched via fetch_price().

    Returns:
        List of OptionsChainSnapshot, one per available expiry.
        Empty list on any failure.
    """
    cache_key = f"{symbol}_all_expiries"
    with _equity_all_expiries_cache_lock:
        if cache_key in _equity_all_expiries_cache:
            cached_time, cached_snapshots = _equity_all_expiries_cache[cache_key]
            age = (datetime.utcnow() - cached_time).total_seconds()
            if age < _EQUITY_OI_CACHE_TTL_SECONDS:
                log.debug(
                    "Equity OI cache hit: %s (age=%.0fs, TTL=%ds)",
                    symbol, age, _EQUITY_OI_CACHE_TTL_SECONDS,
                )
                return cached_snapshots

    log.info("Fetching all-expiries OI chain for equity %s", symbol)

    try:
        ticker = yf.Ticker(symbol)
        available_expiries = list(ticker.options)

        if not available_expiries:
            log.error("No options expiries available for %s", symbol)
            return []

        if spot is None:
            spot = fetch_price(symbol)
            if spot is None:
                log.error("Could not fetch spot price for %s", symbol)
                return []

        log.info(
            "%s: spot=%.2f — scanning %d expiries",
            symbol, spot, len(available_expiries),
        )

        snapshots: list[OptionsChainSnapshot] = []

        for expiry in available_expiries:
            try:
                chain = ticker.option_chain(expiry)
                calls = _normalize_chain(chain.calls)
                puts = _normalize_chain(chain.puts)

                if calls.empty and puts.empty:
                    log.debug("Empty chain for %s expiry %s — skipping", symbol, expiry)
                    continue

                snapshots.append(OptionsChainSnapshot(
                    symbol=symbol,
                    expiry_date=expiry,
                    spot=spot,
                    calls=calls,
                    puts=puts,
                ))
            except Exception:
                log.warning(
                    "Failed to fetch %s expiry %s — skipping",
                    symbol, expiry, exc_info=True,
                )
                continue

        log.info(
            "%s: fetched %d of %d expiry chains",
            symbol, len(snapshots), len(available_expiries),
        )

        with _equity_all_expiries_cache_lock:
            _equity_all_expiries_cache[cache_key] = (datetime.utcnow(), snapshots)

        return snapshots

    except Exception:
        log.exception("Failed to fetch all-expiries chain for %s", symbol)
        return []


# ---------------------------------------------------------------------------
# Lightweight spot/VIX price fetch via raw Yahoo Finance API
# ---------------------------------------------------------------------------

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_price(ticker: str) -> Optional[float]:
    """Fetch the latest closing price for a ticker via Yahoo Finance API.

    Uses the lightweight chart endpoint — no yfinance dependency needed.
    Suitable for SPX spot (^GSPC) and VIX (^VIX) where we only need
    the latest price, not a full options chain.

    Args:
        ticker: Yahoo Finance ticker symbol (e.g. "^GSPC", "^VIX").

    Returns:
        Latest price as float, or None on failure.
    """
    try:
        url = _YAHOO_CHART_URL.format(ticker=ticker.replace("^", "%5E"))
        resp = http_requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        price = round(float([x for x in closes if x is not None][-1]), 2)
        return price
    except Exception as exc:
        log.error("Failed to fetch price for %s: %s", ticker, exc)
        return None
