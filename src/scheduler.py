"""
scheduler.py — Main polling loop for the Options Alert System.

Orchestrates the full pipeline every 60 seconds during market hours:
  1. Fetch SPX spot + VIX
  2. Fetch OI chain (with 5-min cache)
  3. Run OI analysis (Max Pain, GEX, OI concentration)
  4. Evaluate all alert rules
  5. Dedup check (via state_manager stubs — passes all through for now)
  6. Deliver triggered alerts via Telegram
  7. Update in-memory state for cross-detection on next cycle

Market hours: UTC 13:00–21:00 (configurable via env vars).
Keeps previous_gex and previous_vix in memory for cross-detection.

FUTURE EXTENSION: Replace in-memory state with SQLite via state_manager.py Phase 2
FUTURE EXTENSION: Configurable polling interval from alert_config table
FUTURE EXTENSION: Multi-asset support (IBEX 35, etc.) via config/assets.json
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from src.alert_rules import AlertEvent, evaluate_all_alerts
from src.engines.oi_engine import OIAnalysis, analyze_oi
from src.gateways.telegram import send_alert, send_startup_message
from src.market_data import fetch_options_chain, fetch_price

# ---------------------------------------------------------------------------
# state_manager stubs — inlined until Phase 2 SQLite implementation.
# state_manager.py remains as documentation/schema reference only.
# ---------------------------------------------------------------------------


def _was_recently_alerted(alert_type: str, key: str, cooldown_minutes: int = 15) -> bool:
    """Stub: no dedup — all alerts pass through."""
    return False


def _record_alert(event: AlertEvent) -> None:
    """Stub: log only, no persistence."""
    log.info("Alert recorded (stub): type=%s severity=%s", event.alert_type, event.severity)


log = logging.getLogger("options_alert.scheduler")

# ---------------------------------------------------------------------------
# Configuration — from env vars with sensible defaults
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MARKET_OPEN_UTC: int = int(os.getenv("MARKET_OPEN_HOUR_UTC", "13"))
MARKET_CLOSE_UTC: int = int(os.getenv("MARKET_CLOSE_HOUR_UTC", "21"))


class Scheduler:
    """Main polling scheduler that connects all system components.

    Maintains in-memory state for cycle-over-cycle comparisons
    (previous GEX, VIX, OI map) until state_manager.py Phase 2
    provides SQLite-backed persistence.
    """

    def __init__(self) -> None:
        self._previous_gex: Optional[float] = None
        self._previous_vix: Optional[float] = None
        self._previous_oi_map: dict[float, dict] = {}
        self._cycle_count: int = 0

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
        else:
            log.warning("Cycle %d: OI chain unavailable — limited alert set", cycle_id)

        # 4. Build previous-cycle state for cross detection
        previous_gex = self._previous_gex
        previous_vix = self._previous_vix
        previous_oi_map = self._previous_oi_map

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

        # 6. Deliver alerts via Telegram (with dedup check)
        delivered = 0
        for event in alerts:
            dedup_key = self._build_dedup_key(event)

            if _was_recently_alerted(event.alert_type, dedup_key):
                log.info(
                    "Cycle %d: Alert suppressed (cooldown): %s / %s",
                    cycle_id,
                    event.alert_type,
                    dedup_key,
                )
                continue

            if send_alert(event):
                _record_alert(event)
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

        # 7. Update in-memory state for next cycle
        self._previous_vix = vix
        if oi_analysis and oi_analysis.gex:
            self._previous_gex = oi_analysis.gex.net_gex
        if oi_analysis and oi_analysis.oi_concentration:
            self._previous_oi_map = {
                entry.strike: {"call_oi": entry.call_oi, "put_oi": entry.put_oi}
                for entry in oi_analysis.oi_concentration
            }

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
