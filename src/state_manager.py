"""
state_manager.py - Persistencia SQLite para el sistema de alertas de opciones.

Gestiona todas las operaciones de base de datos:
  - Deduplicacion de alertas con ventana de cooldown configurable
  - Registro completo de alertas enviadas (audit trail)
  - Snapshots de OI por strike para comparacion dia-a-dia
  - Resumen de analisis OI por ciclo (Max Pain, GEX, P/C ratio)
  - Configuracion runtime via tabla alert_config

La base de datos se crea en data/options_alert.db (montado como volumen Docker).
Schema changes must be additive only - never DROP or rename columns.
"""

import json
import logging
import os
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from src.alert_rules import AlertEvent

log = logging.getLogger("options_alert.state_manager")

# ---------------------------------------------------------------------------
# Database path - defaults to data/options_alert.db
# ---------------------------------------------------------------------------
_DB_DIR: str = os.getenv("DB_DIR", "data")
_DB_PATH: str = os.path.join(_DB_DIR, "options_alert.db")

# Thread-local storage for connections (one connection per thread)
_local = threading.local()

# ---------------------------------------------------------------------------
# Default seed values for alert_config
# ---------------------------------------------------------------------------
_CONFIG_DEFAULTS: dict[str, tuple[str, str]] = {
    "vix_threshold_1": ("20.0", "Primer umbral VIX"),
    "vix_threshold_2": ("25.0", "Segundo umbral VIX"),
    "vix_threshold_3": ("30.0", "Tercer umbral VIX (critico)"),
    "spot_oi_proximity_points": ("30", "Puntos de proximidad spot-strike para alerta"),
    "maxpain_divergence_points": ("80", "Puntos de divergencia spot vs Max Pain"),
    "oi_buildup_pct": ("20.0", "Porcentaje minimo de incremento OI para alerta"),
    "alert_cooldown_minutes": ("15", "Minutos de cooldown entre alertas duplicadas"),
    "polling_interval_seconds": ("60", "Intervalo de polling en segundos"),
    "market_open_hour_utc": ("13", "Hora apertura mercado (UTC)"),
    "market_close_hour_utc": ("21", "Hora cierre mercado (UTC)"),
}

# ---------------------------------------------------------------------------
# SQL Schema
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oi_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    expiry_date     TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    strike          REAL NOT NULL,
    call_oi         INTEGER NOT NULL DEFAULT 0,
    put_oi          INTEGER NOT NULL DEFAULT 0,
    call_volume     INTEGER NOT NULL DEFAULT 0,
    put_volume      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, expiry_date, snapshot_date, strike)
);

CREATE TABLE IF NOT EXISTS oi_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    expiry_date     TEXT NOT NULL,
    snapshot_ts     TEXT NOT NULL,
    max_pain        REAL,
    net_gex         REAL,
    gex_regime      TEXT,
    pc_ratio        REAL,
    spot            REAL,
    vix             REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    title           TEXT NOT NULL,
    message         TEXT,
    payload_json    TEXT,
    sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
    delivered       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alert_log_dedup
    ON alert_log(alert_type, dedup_key, sent_at);

CREATE TABLE IF NOT EXISTS alert_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Connection management (thread-safe)
# ---------------------------------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """Get or create a SQLite connection for the current thread.

    Returns:
        sqlite3.Connection with WAL mode and foreign keys enabled.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(_DB_DIR, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Initialize the database schema and seed default config values.

    Safe to call multiple times -- uses IF NOT EXISTS and INSERT OR IGNORE.
    """
    conn = _get_connection()
    conn.executescript(_SCHEMA_SQL)

    for key, (value, description) in _CONFIG_DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO alert_config (key, value, description) VALUES (?, ?, ?)",
            (key, value, description),
        )
    conn.commit()
    log.info("Base de datos inicializada: %s", _DB_PATH)


# ---------------------------------------------------------------------------
# Alert dedup / cooldown
# ---------------------------------------------------------------------------

def was_recently_alerted(
    alert_type: str,
    dedup_key: str,
    cooldown_minutes: int = 15,
) -> bool:
    """Check if an alert was recently sent (within cooldown window).

    Args:
        alert_type: The alert type identifier (e.g. "VIX_LEVEL").
        dedup_key: Dedup key for this specific alert instance.
        cooldown_minutes: Minutes within which a duplicate is suppressed.

    Returns:
        True if a matching alert was sent within cooldown window.
    """
    conn = _get_connection()
    row = conn.execute(
        """
        SELECT 1 FROM alert_log
        WHERE alert_type = ? AND dedup_key = ?
          AND sent_at >= datetime('now', ? || ' minutes')
        LIMIT 1
        """,
        (alert_type, dedup_key, str(-cooldown_minutes)),
    ).fetchone()
    return row is not None


def record_alert(event: AlertEvent, dedup_key: str) -> None:
    """Record an alert event to the alert_log table.

    Args:
        event: The AlertEvent to persist.
        dedup_key: The dedup key used for cooldown tracking.
    """
    conn = _get_connection()
    payload_json = json.dumps(event.payload, default=str) if event.payload else None
    conn.execute(
        """
        INSERT INTO alert_log (alert_type, severity, dedup_key, title, message, payload_json, delivered)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            event.alert_type,
            event.severity,
            dedup_key,
            event.title,
            event.message,
            payload_json,
        ),
    )
    conn.commit()
    log.info(
        "Alerta registrada: type=%s key=%s severity=%s",
        event.alert_type,
        dedup_key,
        event.severity,
    )


# ---------------------------------------------------------------------------
# OI Snapshots (day-over-day comparison)
# ---------------------------------------------------------------------------

def save_oi_snapshot(
    symbol: str,
    expiry_date: str,
    snapshot_date: str,
    strikes_data: list[dict[str, Any]],
) -> int:
    """Save raw OI chain data for day-over-day comparison.

    Uses INSERT OR REPLACE to update existing snapshots for the same day.

    Args:
        symbol: Asset symbol (e.g. "^SPX").
        expiry_date: Option expiry date (YYYY-MM-DD).
        snapshot_date: Date of this snapshot (YYYY-MM-DD).
        strikes_data: List of dicts with keys: strike, call_oi, put_oi,
                      call_volume, put_volume.

    Returns:
        Number of rows inserted/updated.
    """
    if not strikes_data:
        return 0

    conn = _get_connection()
    conn.executemany(
        """
        INSERT OR REPLACE INTO oi_snapshots
            (symbol, expiry_date, snapshot_date, strike, call_oi, put_oi, call_volume, put_volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                symbol,
                expiry_date,
                snapshot_date,
                row["strike"],
                row.get("call_oi", 0),
                row.get("put_oi", 0),
                row.get("call_volume", 0),
                row.get("put_volume", 0),
            )
            for row in strikes_data
        ],
    )
    conn.commit()
    log.info(
        "OI snapshot guardado: %s %s %s (%d strikes)",
        symbol,
        expiry_date,
        snapshot_date,
        len(strikes_data),
    )
    return len(strikes_data)


def get_previous_oi_map(symbol: str = "^SPX", expiry_date: Optional[str] = None) -> dict[float, dict]:
    """Retrieve the prior day's OI map for day-over-day comparison.

    Finds the most recent snapshot_date strictly before today, then returns
    all strikes for that date.

    Args:
        symbol: Asset symbol.
        expiry_date: Option expiry date. If None, uses the most recent expiry in DB.

    Returns:
        Dict mapping strike -> {"call_oi": int, "put_oi": int}.
        Empty dict when no prior data exists.
    """
    conn = _get_connection()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if expiry_date is None:
        row = conn.execute(
            "SELECT MAX(expiry_date) as exp FROM oi_snapshots WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None or row["exp"] is None:
            return {}
        expiry_date = row["exp"]

    rows = conn.execute(
        """
        SELECT strike, call_oi, put_oi
        FROM oi_snapshots
        WHERE symbol = ? AND expiry_date = ?
          AND snapshot_date = (
            SELECT MAX(snapshot_date) FROM oi_snapshots
            WHERE symbol = ? AND expiry_date = ? AND snapshot_date < ?
          )
        """,
        (symbol, expiry_date, symbol, expiry_date, today),
    ).fetchall()

    return {
        row["strike"]: {"call_oi": row["call_oi"], "put_oi": row["put_oi"]}
        for row in rows
    }


# ---------------------------------------------------------------------------
# OI Summary (per-cycle analysis results)
# ---------------------------------------------------------------------------

def save_oi_summary(
    symbol: str,
    expiry_date: str,
    spot: float,
    vix: float,
    max_pain: Optional[float] = None,
    net_gex: Optional[float] = None,
    gex_regime: Optional[str] = None,
    pc_ratio: Optional[float] = None,
) -> None:
    """Save computed OI analysis results for the current cycle.

    Args:
        symbol: Asset symbol.
        expiry_date: Option expiry date.
        spot: Current spot price.
        vix: Current VIX level.
        max_pain: Calculated Max Pain strike.
        net_gex: Net GEX value.
        gex_regime: GEX regime string ("POSITIVE" or "NEGATIVE").
        pc_ratio: Put/Call OI ratio.
    """
    conn = _get_connection()
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO oi_summary
            (symbol, expiry_date, snapshot_ts, max_pain, net_gex, gex_regime, pc_ratio, spot, vix)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, expiry_date, snapshot_ts, max_pain, net_gex, gex_regime, pc_ratio, spot, vix),
    )
    conn.commit()


def get_previous_gex() -> Optional[float]:
    """Retrieve the most recent GEX net value from the prior cycle.

    Returns:
        Previous net_gex float, or None if no prior data exists.
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT net_gex FROM oi_summary ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None and row["net_gex"] is not None:
        return float(row["net_gex"])
    return None


def get_previous_vix() -> Optional[float]:
    """Retrieve the most recent VIX value from the prior cycle.

    Returns:
        Previous VIX float, or None if no prior data exists.
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT vix FROM oi_summary ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None and row["vix"] is not None:
        return float(row["vix"])
    return None


# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a runtime config value from alert_config table.

    Args:
        key: Config key to look up.
        default: Fallback value if key not found.

    Returns:
        Config value as string, or default.
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT value FROM alert_config WHERE key = ?",
        (key,),
    ).fetchone()
    if row is not None:
        return row["value"]
    return default


def set_config(key: str, value: str, description: Optional[str] = None) -> None:
    """Set a runtime config value in alert_config table.

    Args:
        key: Config key.
        value: Config value (stored as string).
        description: Optional description of the config key.
    """
    conn = _get_connection()
    conn.execute(
        """
        INSERT INTO alert_config (key, value, description, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = datetime('now')
        """,
        (key, value, description),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def cleanup_old_data(days_to_keep: int = 30) -> None:
    """Remove old snapshots and logs to prevent unbounded DB growth.

    Args:
        days_to_keep: Number of days of data to retain.
    """
    conn = _get_connection()
    cutoff = f"-{days_to_keep} days"

    deleted_snapshots = conn.execute(
        "DELETE FROM oi_snapshots WHERE created_at < datetime('now', ?)",
        (cutoff,),
    ).rowcount
    deleted_summaries = conn.execute(
        "DELETE FROM oi_summary WHERE created_at < datetime('now', ?)",
        (cutoff,),
    ).rowcount
    deleted_alerts = conn.execute(
        "DELETE FROM alert_log WHERE sent_at < datetime('now', ?)",
        (cutoff,),
    ).rowcount
    conn.commit()

    if deleted_snapshots or deleted_summaries or deleted_alerts:
        log.info(
            "Limpieza BD: eliminados %d snapshots, %d summaries, %d alertas (>%d dias)",
            deleted_snapshots,
            deleted_summaries,
            deleted_alerts,
            days_to_keep,
        )


def get_oi_first_seen_map(symbol: str) -> dict:
    """Return the earliest snapshot_date where OI > 0 for each (expiry, strike, side).

    Used to show how long an unusual OTM OI position has existed.

    Args:
        symbol: Equity ticker symbol.

    Returns:
        Nested dict: {expiry_date: {strike: {"call_first_seen": str|None, "put_first_seen": str|None}}}
    """
    conn = _get_connection()
    rows = conn.execute(
        """
        SELECT
            expiry_date,
            strike,
            MIN(CASE WHEN call_oi > 0 THEN snapshot_date END) AS call_first_seen,
            MIN(CASE WHEN put_oi  > 0 THEN snapshot_date END) AS put_first_seen
        FROM oi_snapshots
        WHERE symbol = ?
        GROUP BY expiry_date, strike
        """,
        (symbol,),
    ).fetchall()

    result: dict = {}
    for row in rows:
        expiry = row["expiry_date"]
        strike = row["strike"]
        if expiry not in result:
            result[expiry] = {}
        result[expiry][strike] = {
            "call_first_seen": row["call_first_seen"],
            "put_first_seen": row["put_first_seen"],
        }
    return result
