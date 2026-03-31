"""
scheduler.py — Main polling loop for the Options Alert System.

Orchestrates the full pipeline every 60 seconds during market hours:
  1. Fetch SPX spot + VIX
  2. Fetch OI chain (with 5-min cache)
  3. Run OI analysis (Max Pain, GEX, OI concentration)
  4. Save OI snapshot + summary to SQLite
  5. Evaluate all alert rules (with persistent previous state)
  6. Dedup check (15-min cooldown via alert_log)
  7. Deliver triggered alerts via Telegram
  8. Record delivered alerts to SQLite

Market hours: UTC 13:00-21:00 (configurable via env vars or alert_config).
Previous GEX/VIX/OI state is loaded from SQLite on startup (survives restarts).

FUTURE EXTENSION: Configurable polling interval from alert_config table
FUTURE EXTENSION: Multi-asset support (IBEX 35, etc.) via config/assets.json
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from src.alert_rules import AlertEvent, evaluate_all_alerts, check_unusual_otm_oi
from src.state_manager import get_oi_first_seen_map
from src.engines.oi_engine import OIAnalysis, analyze_oi
from src.gateways.telegram import send_alert, send_startup_message
from src.market_data import fetch_options_chain, fetch_price, fetch_all_expiries_chain
from src import state_manager


log = logging.getLogger("options_alert.scheduler")

# ---------------------------------------------------------------------------
# Configuration — from env vars with sensible defaults
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MARKET_OPEN_UTC: int = int(os.getenv("MARKET_OPEN_HOUR_UTC", "13"))
MARKET_CLOSE_UTC: int = int(os.getenv("MARKET_CLOSE_HOUR_UTC", "21"))


class Scheduler:
    """Main polling scheduler that connects all system components.

    Uses SQLite-backed state_manager for persistence across restarts.
    Previous GEX, VIX, and OI maps are loaded from the database.
    """

    def __init__(self) -> None:
        self._cycle_count: int = 0
        self._equity_cycle_counter: int = 0
        self._assets_config: dict = self._load_assets_config()
        # Initialize SQLite database on startup
        state_manager.init_db()
        log.info("State manager inicializado con SQLite")

    # -------------------------------------------------------------------
    # Market hours check
    # -------------------------------------------------------------------

    def _is_market_hours(self) -> bool:
        """Check if current UTC time is within configured market hours."""
        now = datetime.now(timezone.utc)
        return MARKET_OPEN_UTC <= now.hour < MARKET_CLOSE_UTC

    # -------------------------------------------------------------------
    # Days to expiry helper
    # -------------------------------------------------------------------

    @staticmethod
    def _compute_days_to_expiry(expiry_date: str) -> int:
        """Compute calendar days from today to expiry date.

        Args:
            expiry_date: Expiry date string in YYYY-MM-DD format.

        Returns:
            Non-negative integer of calendar days to expiry.
        """
        try:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            return max((expiry - today).days, 0)
        except ValueError:
            log.warning("Could not parse expiry date '%s' — defaulting to 99", expiry_date)
            return 99

    # -------------------------------------------------------------------
    # Dedup key builder
    # -------------------------------------------------------------------

    @staticmethod
    def _build_dedup_key(event: AlertEvent) -> str:
        """Build a dedup key for the cooldown check.

        Each alert type has a specific key pattern so that related
        but distinct alerts (e.g. different VIX thresholds) are
        tracked independently.

        Args:
            event: The alert event to build a key for.

        Returns:
            String dedup key.
        """
        payload = event.payload

        if event.alert_type == "GEX_FLIP_NEGATIVE":
            return "gex_flip"
        elif event.alert_type == "SPOT_OI_PROXIMITY":
            return f"proximity_{payload.get('strike', 0):.0f}"
        elif event.alert_type == "VIX_LEVEL":
            return f"vix_{payload.get('threshold', 0):.0f}"
        elif event.alert_type == "MAXPAIN_DIVERGENCE":
            return "maxpain_div"
        elif event.alert_type == "OI_BUILDUP":
            return f"buildup_{payload.get('strike', 0):.0f}_{payload.get('side', 'TOTAL')}"
        elif event.alert_type == "UNUSUAL_OTM_OI":
            return f"unusual_oi_{payload.get('symbol', 'unknown')}_{payload.get('side', 'unknown')}"

        return f"unknown_{event.alert_type}"

    # -------------------------------------------------------------------
    # Single evaluation cycle
    # -------------------------------------------------------------------

    def run_cycle(self) -> None:
        """Execute one full evaluation cycle.

        Fetches data, runs analysis, evaluates alerts, and delivers
        notifications. Safe to call repeatedly — all errors are caught
        and logged without crashing.
        """
        self._cycle_count += 1
        cycle_id = self._cycle_count
        log.info("=== Cycle %d starting ===", cycle_id)

        # 1. Fetch SPX spot + VIX
        spot = fetch_price("^GSPC")
        vix = fetch_price("^VIX")

        if spot is None or vix is None:
            log.warning(
                "Cycle %d: Failed to fetch market data (spot=%s, vix=%s) — skipping",
                cycle_id,
                spot,
                vix,
            )
            return

        log.info("Cycle %d: SPX=%.2f  VIX=%.2f", cycle_id, spot, vix)

        # 2. Fetch OI chain (5-min cache inside market_data.py)
        chain = fetch_options_chain(symbol="^SPX", spot=spot)

        # 3. Run OI analysis
        oi_analysis: Optional[OIAnalysis] = None
        if chain is not None:
            oi_analysis = analyze_oi(
                calls=chain.calls,
                puts=chain.puts,
                spot=chain.spot,
                expiry_date=chain.expiry_date,
                symbol=chain.symbol,
            )
            if oi_analysis:
                log.info(
                    "Cycle %d: OI analysis OK — MaxPain=%.0f  GEX=%s  DTE=%d",
                    cycle_id,
                    oi_analysis.max_pain.strike,
                    oi_analysis.gex.regime,
                    self._compute_days_to_expiry(chain.expiry_date),
                )

                # Save OI snapshot to SQLite for day-over-day comparison
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                strikes_data = [
                    {
                        "strike": entry.strike,
                        "call_oi": entry.call_oi,
                        "put_oi": entry.put_oi,
                        "call_volume": 0,
                        "put_volume": 0,
                    }
                    for entry in oi_analysis.oi_concentration
                ]
                state_manager.save_oi_snapshot(
                    symbol=chain.symbol,
                    expiry_date=chain.expiry_date,
                    snapshot_date=today_str,
                    strikes_data=strikes_data,
                )

                # Save OI summary to SQLite
                state_manager.save_oi_summary(
                    symbol=chain.symbol,
                    expiry_date=chain.expiry_date,
                    spot=spot,
                    vix=vix,
                    max_pain=oi_analysis.max_pain.strike,
                    net_gex=oi_analysis.gex.net_gex,
                    gex_regime=oi_analysis.gex.regime,
                    pc_ratio=oi_analysis.gex.net_gex,  # FUTURE EXTENSION: compute real P/C ratio
                )
        else:
            log.warning("Cycle %d: OI chain unavailable — limited alert set", cycle_id)

        # 4. Load previous-cycle state from SQLite for cross detection
        previous_gex = state_manager.get_previous_gex()
        previous_vix = state_manager.get_previous_vix()
        previous_oi_map = state_manager.get_previous_oi_map(
            symbol=chain.symbol if chain else "^SPX",
            expiry_date=chain.expiry_date if chain else None,
        )

        # 5. Evaluate all alerts
        alerts = evaluate_all_alerts(
            spot=spot,
            vix=vix,
            previous_vix=previous_vix if previous_vix is not None else vix,
            gex=oi_analysis.gex if oi_analysis else None,
            previous_gex=previous_gex,
            max_pain=oi_analysis.max_pain if oi_analysis else None,
            days_to_expiry=(
                self._compute_days_to_expiry(chain.expiry_date)
                if chain else 99
            ),
            oi_concentration=oi_analysis.oi_concentration if oi_analysis else [],
            previous_oi_map=previous_oi_map,
        )

        # 6. Deliver alerts via Telegram (with SQLite dedup check)
        cooldown = int(state_manager.get_config("alert_cooldown_minutes", "15"))
        delivered = 0
        for event in alerts:
            dedup_key = self._build_dedup_key(event)

            if state_manager.was_recently_alerted(event.alert_type, dedup_key, cooldown):
                log.info(
                    "Cycle %d: Alert suppressed (cooldown %dm): %s / %s",
                    cycle_id,
                    cooldown,
                    event.alert_type,
                    dedup_key,
                )
                continue

            if send_alert(event):
                state_manager.record_alert(event, dedup_key)
                delivered += 1
            else:
                log.warning(
                    "Cycle %d: Failed to deliver alert: %s",
                    cycle_id,
                    event.alert_type,
                )

        log.info(
            "=== Cycle %d complete: %d triggered, %d delivered ===",
            cycle_id,
            len(alerts),
            delivered,
        )

        # Equity unusual OI scan — runs every N cycles (configurable)
        self._equity_cycle_counter += 1
        scan_interval = int(
            self._assets_config.get("unusual_oi", {}).get("scan_interval_cycles", 5)
        )
        if self._equity_cycle_counter >= scan_interval:
            self._equity_cycle_counter = 0
            cooldown = int(state_manager.get_config("alert_cooldown_minutes", "15"))
            self._run_equity_scan(cooldown)

    # -------------------------------------------------------------------
    # Equity unusual OI scan
    # -------------------------------------------------------------------

    @staticmethod
    def _load_assets_config() -> dict:
        """Load equity watchlist and thresholds from config/assets.json."""
        config_path = "config/assets.json"
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            log.info(
                "Assets config loaded: %d equity tickers",
                len(cfg.get("equity_options_watchlist", [])),
            )
            return cfg
        except FileNotFoundError:
            log.warning("config/assets.json not found — equity scan disabled")
            return {}
        except Exception:
            log.exception("Failed to load config/assets.json")
            return {}

    def _run_equity_scan(self, cooldown: int) -> None:
        """Scan all enabled equity tickers for unusual deep-OTM OI."""
        watchlist = self._assets_config.get("equity_options_watchlist", [])
        oi_cfg = self._assets_config.get("unusual_oi", {})
        min_oi = int(oi_cfg.get("min_oi_contracts", 30000))
        min_otm_pct = float(oi_cfg.get("min_otm_pct", 80.0))

        # [TRADING IMPLICATION]: OI es dato diario — cooldown 24h por (symbol, side)
        oi_cooldown_minutes = 24 * 60

        enabled = [a for a in watchlist if a.get("enabled", True)]
        log.info("Equity scan starting: %d tickers", len(enabled))

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for asset in enabled:
            symbol = asset["symbol"]
            try:
                spot = fetch_price(symbol)
                if spot is None:
                    log.warning("Equity scan: no spot for %s — skipping", symbol)
                    continue

                chains = fetch_all_expiries_chain(symbol=symbol, spot=spot)
                if not chains:
                    log.warning("Equity scan: no chain data for %s — skipping", symbol)
                    continue

                # Load previous OI maps per expiry BEFORE saving today's snapshots
                previous_oi_by_expiry: dict = {}
                for snapshot in chains:
                    prev = state_manager.get_previous_oi_map(
                        symbol=symbol,
                        expiry_date=snapshot.expiry_date,
                    )
                    if prev:
                        previous_oi_by_expiry[snapshot.expiry_date] = prev

                # Save today's OTM-only snapshots (OI > 0) for tomorrow's comparison
                for snapshot in chains:
                    spot_snap = snapshot.spot
                    strikes: dict[float, dict] = {}
                    for _, row in snapshot.calls[snapshot.calls["strike"] > spot_snap].iterrows():
                        oi = int(row["openInterest"])
                        if oi <= 0:
                            continue
                        s = float(row["strike"])
                        strikes.setdefault(s, {"strike": s, "call_oi": 0, "put_oi": 0, "call_volume": 0, "put_volume": 0})
                        strikes[s]["call_oi"] = oi
                        strikes[s]["call_volume"] = int(row.get("volume", 0) or 0)
                    for _, row in snapshot.puts[snapshot.puts["strike"] < spot_snap].iterrows():
                        oi = int(row["openInterest"])
                        if oi <= 0:
                            continue
                        s = float(row["strike"])
                        strikes.setdefault(s, {"strike": s, "call_oi": 0, "put_oi": 0, "call_volume": 0, "put_volume": 0})
                        strikes[s]["put_oi"] = oi
                        strikes[s]["put_volume"] = int(row.get("volume", 0) or 0)
                    if strikes:
                        state_manager.save_oi_snapshot(
                            symbol=symbol,
                            expiry_date=snapshot.expiry_date,
                            snapshot_date=today_str,
                            strikes_data=list(strikes.values()),
                        )

                first_seen_map = get_oi_first_seen_map(symbol)

                equity_alerts = check_unusual_otm_oi(
                    symbol=symbol,
                    spot=spot,
                    expiry_chains=chains,
                    previous_oi_by_expiry=previous_oi_by_expiry,
                    first_seen_map=first_seen_map,
                    min_oi=min_oi,
                    min_otm_pct=min_otm_pct,
                )

                for event in equity_alerts:
                    dedup_key = self._build_dedup_key(event)
                    if state_manager.was_recently_alerted(event.alert_type, dedup_key, oi_cooldown_minutes):
                        log.info(
                            "Equity alert suppressed (cooldown 24h): %s / %s",
                            event.alert_type, dedup_key,
                        )
                        continue
                    if send_alert(event):
                        state_manager.record_alert(event, dedup_key)

            except Exception:
                log.exception("Equity scan failed for %s", symbol)

        log.info("Equity scan complete")

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def run(self) -> None:
        """Start the infinite polling loop.

        Sends a startup message, then polls every POLL_INTERVAL_SECONDS.
        Outside market hours, sleeps without fetching data.
        Catches all exceptions to keep the loop alive.
        """
        log.info(
            "Scheduler starting — poll=%ds, market hours=%02d:00-%02d:00 UTC",
            POLL_INTERVAL_SECONDS,
            MARKET_OPEN_UTC,
            MARKET_CLOSE_UTC,
        )

        # Send startup message with current market data
        try:
            spot = fetch_price("^GSPC")
            vix = fetch_price("^VIX")
            send_startup_message(spot=spot, vix=vix)
        except Exception:
            log.exception("Failed to send startup message — continuing anyway")

        # Run equity scan immediately on startup
        cooldown_startup = int(state_manager.get_config("alert_cooldown_minutes", "15"))
        try:
            self._run_equity_scan(cooldown_startup)
        except Exception:
            log.exception("Startup equity scan failed — continuing anyway")

        # Main loop
        while True:
            if not self._is_market_hours():
                now = datetime.now(timezone.utc)
                log.debug(
                    "Outside market hours (%02d:%02d UTC) — sleeping %ds",
                    now.hour,
                    now.minute,
                    POLL_INTERVAL_SECONDS,
                )
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            try:
                self.run_cycle()
            except Exception:
                log.exception("Unhandled error in scheduler cycle — will retry next interval")

            time.sleep(POLL_INTERVAL_SECONDS)
