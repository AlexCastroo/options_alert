"""
state_manager.py — Persistence and state management for the Options Alert System.

STUB IMPLEMENTATION — Phase 1.
This file provides the interface that the scheduler needs to run TODAY,
with all methods returning safe defaults (no-op). The full SQLite-backed
implementation will be built in Phase 2.

# ==========================================================================
# PHASE 2: FULL SQLITE IMPLEMENTATION — PLANNED SCHEMA
# ==========================================================================
#
# --- Table: oi_snapshots ---
# Stores raw OI chain data per expiry per day for day-over-day comparison.
#
#   CREATE TABLE IF NOT EXISTS oi_snapshots (
#       id              INTEGER PRIMARY KEY AUTOINCREMENT,
#       symbol          TEXT NOT NULL,
#       expiry_date     TEXT NOT NULL,
#       snapshot_date   TEXT NOT NULL,
#       strike          REAL NOT NULL,
#       call_oi         INTEGER NOT NULL DEFAULT 0,
#       put_oi          INTEGER NOT NULL DEFAULT 0,
#       call_volume     INTEGER NOT NULL DEFAULT 0,
#       put_volume      INTEGER NOT NULL DEFAULT 0,
#       created_at      TEXT NOT NULL DEFAULT (datetime('now')),
#       UNIQUE(symbol, expiry_date, snapshot_date, strike)
#   );
#
# --- Table: oi_summary ---
# Stores computed OI analysis results (Max Pain, GEX, P/C ratio) per cycle.
#
#   CREATE TABLE IF NOT EXISTS oi_summary (
#       id              INTEGER PRIMARY KEY AUTOINCREMENT,
#       symbol          TEXT NOT NULL,
#       expiry_date     TEXT NOT NULL,
#       snapshot_ts     TEXT NOT NULL,
#       max_pain        REAL,
#       net_gex         REAL,
#       gex_regime      TEXT,
#       pc_ratio        REAL,
#       spot            REAL,
#       vix             REAL,
#       created_at      TEXT NOT NULL DEFAULT (datetime('now'))
#   );
#
# --- Table: alert_log ---
# Full payload JSON for every alert sent, for audit and dedup.
#
#   CREATE TABLE IF NOT EXISTS alert_log (
#       id              INTEGER PRIMARY KEY AUTOINCREMENT,
#       alert_type      TEXT NOT NULL,
#       severity        TEXT NOT NULL,
#       dedup_key       TEXT NOT NULL,
#       title           TEXT NOT NULL,
#       message         TEXT,
#       payload_json    TEXT,
#       sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
#       delivered       INTEGER NOT NULL DEFAULT 0
#   );
#   CREATE INDEX IF NOT EXISTS idx_alert_log_dedup
#       ON alert_log(alert_type, dedup_key, sent_at);
#
# --- Table: alert_config ---
# Runtime-configurable key/value pairs. Seeded with defaults on first run.
#
#   CREATE TABLE IF NOT EXISTS alert_config (
#       key             TEXT PRIMARY KEY,
#       value           TEXT NOT NULL,
#       description     TEXT,
#       updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
#   );
#
#   -- Seed values (INSERT OR IGNORE):
#   --   vix_threshold_1            = 20.0
#   --   vix_threshold_2            = 25.0
#   --   vix_threshold_3            = 30.0
#   --   spot_oi_proximity_points   = 30
#   --   maxpain_divergence_points  = 80
#   --   oi_buildup_pct             = 20.0
#   --   alert_cooldown_minutes     = 15
#   --   polling_interval_seconds   = 60
#   --   market_open_hour_utc       = 13
#   --   market_close_hour_utc      = 21
#
# ==========================================================================
# PHASE 2: ALERT COOLDOWN / DEDUP LOGIC (PLANNED)
# ==========================================================================
#
# The was_recently_alerted() method will query alert_log:
#   SELECT 1 FROM alert_log
#   WHERE alert_type = ? AND dedup_key = ?
#     AND sent_at >= datetime('now', '-{cooldown_minutes} minutes')
#   LIMIT 1;
#
# The dedup_key is constructed per alert type:
#   - GEX_FLIP_NEGATIVE:  "gex_flip"  (one key — only fires on cross)
#   - SPOT_OI_PROXIMITY:  "proximity_{strike}"  (per strike)
#   - VIX_LEVEL:          "vix_{threshold}"  (per threshold level)
#   - MAXPAIN_DIVERGENCE: "maxpain_div"  (one key)
#   - OI_BUILDUP:         "buildup_{strike}_{side}"  (per strike+side)
#
# ==========================================================================
# PHASE 2: DAY-OVER-DAY OI COMPARISON (PLANNED)
# ==========================================================================
#
# get_previous_oi_map() will query oi_snapshots for the most recent
# snapshot_date that is strictly before today, grouped by strike:
#
#   SELECT strike, call_oi, put_oi
#   FROM oi_snapshots
#   WHERE symbol = ? AND expiry_date = ?
#     AND snapshot_date = (
#       SELECT MAX(snapshot_date) FROM oi_snapshots
#       WHERE symbol = ? AND expiry_date = ? AND snapshot_date < date('now')
#     );
#
# Returns dict[float, dict] for check_oi_buildup() consumption.
#
# ==========================================================================
# PHASE 2: RUNTIME CONFIG (PLANNED)
# ==========================================================================
#
# get_config(key, default) will read from alert_config table, falling back
# to the provided default if the key doesn't exist. This allows threshold
# tuning without restarting the process.
#
# ==========================================================================

import logging
from typing import Optional

from src.alert_rules import AlertEvent

log = logging.getLogger("options_alert.state_manager")


def was_recently_alerted(
    alert_type: str,
    key: str,
    cooldown_minutes: int = 15,
) -> bool:
    """Check if an alert was recently sent (within cooldown window).

    # TODO: PHASE 2 — Query alert_log table with cooldown window.
    # Currently returns False always so all alerts pass through.

    Args:
        alert_type: The alert type identifier (e.g. "VIX_LEVEL").
        key: Dedup key for this specific alert instance.
        cooldown_minutes: Minutes within which a duplicate is suppressed.

    Returns:
        True if a matching alert was sent within cooldown window, False otherwise.
    """
    # TODO: PHASE 2 — implement SQLite query against alert_log
    log.debug(
        "was_recently_alerted STUB: alert_type=%s, key=%s, cooldown=%dm -> False",
        alert_type,
        key,
        cooldown_minutes,
    )
    return False


def record_alert(event: AlertEvent) -> None:
    """Record an alert event to persistent storage.

    # TODO: PHASE 2 — INSERT into alert_log with full payload JSON.

    Args:
        event: The AlertEvent to persist.
    """
    # TODO: PHASE 2 — implement SQLite INSERT into alert_log
    log.info(
        "record_alert STUB: type=%s severity=%s title=%s",
        event.alert_type,
        event.severity,
        event.title,
    )


def get_previous_gex() -> Optional[float]:
    """Retrieve the most recent GEX net value from prior cycle.

    # TODO: PHASE 2 — Query oi_summary for the latest net_gex value
    # from a prior cycle (not the current one).

    Returns:
        Previous net_gex float, or None if no prior data exists.
    """
    # TODO: PHASE 2 — implement SQLite query against oi_summary
    log.debug("get_previous_gex STUB: returning None")
    return None


def get_previous_vix() -> Optional[float]:
    """Retrieve the most recent VIX value from prior cycle.

    # TODO: PHASE 2 — Query market_snapshots for the latest VIX value
    # from a prior cycle.

    Returns:
        Previous VIX float, or None if no prior data exists.
    """
    # TODO: PHASE 2 — implement SQLite query against market_snapshots
    log.debug("get_previous_vix STUB: returning None")
    return None


def get_previous_oi_map() -> dict:
    """Retrieve the prior day's OI map for day-over-day comparison.

    # TODO: PHASE 2 — Query oi_snapshots for the most recent snapshot_date
    # before today, returning {strike: {"call_oi": int, "put_oi": int}}.

    Returns:
        Dict mapping strike -> {"call_oi": int, "put_oi": int}.
        Empty dict when no prior data exists (baseline not yet established).
    """
    # TODO: PHASE 2 — implement SQLite query against oi_snapshots
    log.debug("get_previous_oi_map STUB: returning empty dict")
    return {}
