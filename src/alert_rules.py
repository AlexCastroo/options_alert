"""
alert_rules.py — Pure trigger logic for the Options Alert System.

Every alert is a standalone function: typed inputs in, AlertEvent out.
No I/O, no database, no network calls. State management (cooldown, dedup)
is handled by state_manager.py upstream — this module fires if conditions
are met, nothing more.

Architecture:
  - Registry pattern via ALERT_REGISTRY dict (no if/else branching)
  - Each alert function returns Optional[AlertEvent] or list[AlertEvent]
  - evaluate_all_alerts() is the single entry point the scheduler calls

Current alerts:
  1. GEX_FLIP_NEGATIVE   — GEX regime change from positive to negative
  2. SPOT_OI_PROXIMITY    — Spot approaching high-OI strike
  3. VIX_LEVEL            — VIX crosses configurable threshold upward
  4. MAXPAIN_DIVERGENCE   — Spot diverged from Max Pain near expiry
  5. OI_BUILDUP           — Significant OI increase at a strike vs prior day

FUTURE EXTENSION: VOLUME_SPIKE — unusual intraday volume at a strike
FUTURE EXTENSION: IV_SKEW_SHIFT — put/call IV skew regime change
FUTURE EXTENSION: TERM_STRUCTURE_INVERSION — near-term IV > far-term IV
"""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable, Optional, Union

from src.engines.oi_engine import GEXResult, MaxPainResult, OIConcentration

log = logging.getLogger("options_alert.alert_rules")


# ---------------------------------------------------------------------------
# AlertEvent — the universal output contract for all alert functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertEvent:
    """Immutable alert event produced by any alert rule.

    This is the single data contract between alert_rules and everything
    downstream (state_manager for dedup/logging, gateways for delivery).

    Attributes:
        alert_type: Machine-readable alert identifier (e.g. "GEX_FLIP_NEGATIVE").
        severity: "INFO", "WARNING", or "CRITICAL".
        title: Short title for Telegram message header.
        message: Full human-readable message in trader language.
        payload: Structured data dict for persistence and downstream consumers.
        triggered_at: UTC timestamp of when the alert was evaluated.
    """

    alert_type: str
    severity: str
    title: str
    message: str
    payload: dict = field(default_factory=dict)
    triggered_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Type alias for alert functions
# ---------------------------------------------------------------------------

AlertFunction = Callable[..., Union[Optional[AlertEvent], list[AlertEvent]]]


# ---------------------------------------------------------------------------
# 1. GEX_FLIP_NEGATIVE
# ---------------------------------------------------------------------------


def check_gex_flip_negative(
    gex: GEXResult,
    previous_gex: float,
) -> Optional[AlertEvent]:
    """Detect GEX regime change from positive to negative.

    [TRADING IMPLICATION]: When aggregate GEX crosses from positive to
    negative, market makers transition from dampening moves (buying dips,
    selling rips) to amplifying them (selling into drops, buying into rips).
    This is the single most favorable regime for directional option buying
    (long premium). Fire ONCE on the cross, not every cycle while negative.

    Args:
        gex: Current GEX calculation result from oi_engine.
        previous_gex: The net_gex value from the prior evaluation cycle.

    Returns:
        AlertEvent if GEX flipped from positive to negative, None otherwise.
    """
    if gex is None:
        log.debug("GEX_FLIP_NEGATIVE: no GEX data — skipping")
        return None

    current_gex = gex.net_gex

    # Guard: need a valid previous reading to detect a cross
    if previous_gex is None:
        log.debug("GEX_FLIP_NEGATIVE: no previous GEX reading — skipping")
        return None

    # Detect the cross: previous >= 0 AND current < 0
    if not (previous_gex >= 0 and current_gex < 0):
        return None

    flip_strike_str = f"{gex.flip_strike:.0f}" if gex.flip_strike is not None else "N/A"
    net_gex_millions = current_gex / 1_000_000

    message = (
        f"GEX paso a NEGATIVO -- dealers ahora amplifican movimientos. "
        f"GEX Neto: {net_gex_millions:+.1f}M. "
        f"Strike de flip: {flip_strike_str}. "
        f"GEX anterior: {previous_gex / 1_000_000:+.1f}M. "
        f"Regimen: lateral -> TENDENCIAL."
    )

    log.info("GEX_FLIP_NEGATIVE triggered: net_gex=%.0f, previous=%.0f", current_gex, previous_gex)

    return AlertEvent(
        alert_type="GEX_FLIP_NEGATIVE",
        severity="CRITICAL",
        title="Cambio de Regimen GEX: NEGATIVO",
        message=message,
        payload={
            "net_gex": current_gex,
            "net_gex_millions": round(net_gex_millions, 2),
            "previous_gex": previous_gex,
            "previous_gex_millions": round(previous_gex / 1_000_000, 2),
            "flip_strike": gex.flip_strike,
            "call_gex": gex.call_gex,
            "put_gex": gex.put_gex,
            "regime": gex.regime,
            "spot": gex.spot,
        },
    )


# ---------------------------------------------------------------------------
# 2. SPOT_OI_PROXIMITY
# ---------------------------------------------------------------------------


def check_spot_oi_proximity(
    spot: float,
    oi_concentration: list[OIConcentration],
    proximity_points: float = 30.0,
    top_n_strikes: int = 3,
) -> list[AlertEvent]:
    """Detect spot price approaching high-OI strikes.

    [TRADING IMPLICATION]: High-OI strikes act as magnets or walls.
    A put-dominated strike below spot = support/magnet (dealers long puts,
    hedging creates buying pressure). A call-dominated strike above spot =
    resistance/cap. Alert fires when approaching — re-alerts if price
    moves away and returns. Cooldown is handled by state_manager upstream.

    Args:
        spot: Current SPX spot price.
        oi_concentration: Ranked list of OIConcentration from oi_engine.
        proximity_points: Distance threshold in index points. Default 30.
        top_n_strikes: Number of top OI strikes to monitor. Default 3.

    Returns:
        List of AlertEvents (one per qualifying strike). May be empty.
    """
    if spot <= 0:
        log.debug("SPOT_OI_PROXIMITY: invalid spot %.2f — skipping", spot)
        return []

    if not oi_concentration:
        log.debug("SPOT_OI_PROXIMITY: no OI concentration data — skipping")
        return []

    alerts: list[AlertEvent] = []
    monitored_strikes = oi_concentration[:top_n_strikes]

    for oi_entry in monitored_strikes:
        distance = spot - oi_entry.strike
        abs_distance = abs(distance)

        if abs_distance > proximity_points:
            continue

        # Direction: approaching from above (spot > strike) or below
        if distance > 0:
            direction = "ABOVE"
            approach_desc = "acercandose desde arriba"
        elif distance < 0:
            direction = "BELOW"
            approach_desc = "acercandose desde abajo"
        else:
            direction = "AT"
            approach_desc = "justo en"

        # Determine call/put dominance at this strike
        if oi_entry.put_oi > oi_entry.call_oi:
            dominance = "PUT"
            wall_type = "Muro Put"
            # [TRADING IMPLICATION]: put wall below = support/magnet
            implication = "soporte/iman -- dealers cubriendo puts generan presion compradora"
            if direction == "BELOW":
                implication = "resistencia desde abajo -- precio empujando hacia concentracion de puts"
        else:
            dominance = "CALL"
            wall_type = "Muro Call"
            # [TRADING IMPLICATION]: call wall above = resistance/cap
            implication = "resistencia/techo -- dealers cubriendo calls generan presion vendedora"
            if direction == "ABOVE":
                implication = "soporte desde arriba -- precio retrocediendo hacia concentracion de calls"

        severity = "WARNING" if abs_distance <= proximity_points / 2 else "INFO"

        message = (
            f"Spot a {abs_distance:.0f}pts del {wall_type} en {oi_entry.strike:.0f} "
            f"(OI: {oi_entry.total_oi:,}). "
            f"{approach_desc.capitalize()} -- {implication}."
        )

        log.info(
            "SPOT_OI_PROXIMITY triggered: strike=%.0f, distance=%.0f, dominance=%s",
            oi_entry.strike,
            abs_distance,
            dominance,
        )

        alerts.append(AlertEvent(
            alert_type="SPOT_OI_PROXIMITY",
            severity=severity,
            title=f"Spot Cerca de {wall_type}: {oi_entry.strike:.0f}",
            message=message,
            payload={
                "spot": spot,
                "strike": oi_entry.strike,
                "distance": round(distance, 2),
                "abs_distance": round(abs_distance, 2),
                "direction": direction,
                "call_oi": oi_entry.call_oi,
                "put_oi": oi_entry.put_oi,
                "total_oi": oi_entry.total_oi,
                "pct_of_total": oi_entry.pct_of_total,
                "dominance": dominance,
                "proximity_points": proximity_points,
            },
        ))

    return alerts


# ---------------------------------------------------------------------------
# 3. VIX_LEVEL
# ---------------------------------------------------------------------------

# Default VIX thresholds with severity mapping
_VIX_THRESHOLDS_DEFAULT: list[tuple[float, str]] = [
    (30.0, "CRITICAL"),
    (25.0, "WARNING"),
    (20.0, "INFO"),
]


def check_vix_level(
    vix: float,
    previous_vix: float,
    thresholds: Optional[list[tuple[float, str]]] = None,
) -> list[AlertEvent]:
    """Detect VIX crossing configurable thresholds upward.

    [TRADING IMPLICATION]: VIX is a pure market condition signal.
    VIX > 20 = elevated uncertainty. VIX > 25 = stress. VIX > 30 = fear.
    Only fires on UPWARD crosses to avoid spam while VIX stays elevated.
    Each threshold fires independently — crossing 30 also fires 25 and 20
    if all were crossed in the same cycle.

    Args:
        vix: Current VIX value.
        previous_vix: VIX value from the prior evaluation cycle.
        thresholds: List of (threshold, severity) tuples, sorted descending.
            Defaults to [(30, CRITICAL), (25, WARNING), (20, INFO)].

    Returns:
        List of AlertEvents (one per threshold crossed). May be empty.
    """
    if vix is None or previous_vix is None:
        log.debug("VIX_LEVEL: missing VIX data (current=%s, previous=%s) — skipping", vix, previous_vix)
        return []

    if vix <= 0 or previous_vix <= 0:
        log.debug("VIX_LEVEL: invalid VIX values (current=%.2f, previous=%.2f) — skipping", vix, previous_vix)
        return []

    if thresholds is None:
        thresholds = _VIX_THRESHOLDS_DEFAULT

    alerts: list[AlertEvent] = []

    for threshold, severity in thresholds:
        # Upward cross: previous was below, current is at or above
        if previous_vix < threshold <= vix:
            message = (
                f"VIX cruzo {threshold:.0f} (ahora {vix:.1f}). "
                f"Anterior: {previous_vix:.1f}. "
            )

            if severity == "CRITICAL":
                message += "Miedo elevado en el mercado -- entorno de primas altas."
            elif severity == "WARNING":
                message += "Estres del mercado escalando -- movimientos direccionales probables."
            else:
                message += "Volatilidad en aumento -- monitorear por continuacion."

            log.info(
                "VIX_LEVEL triggered: vix=%.1f crossed %.0f (previous=%.1f, severity=%s)",
                vix,
                threshold,
                previous_vix,
                severity,
            )

            alerts.append(AlertEvent(
                alert_type="VIX_LEVEL",
                severity=severity,
                title=f"VIX Cruzo {threshold:.0f}",
                message=message,
                payload={
                    "vix": vix,
                    "previous_vix": previous_vix,
                    "threshold": threshold,
                    "severity": severity,
                },
            ))

    return alerts


# ---------------------------------------------------------------------------
# 4. MAXPAIN_DIVERGENCE
# ---------------------------------------------------------------------------


def check_maxpain_divergence(
    spot: float,
    max_pain: MaxPainResult,
    days_to_expiry: int,
    divergence_points: float = 80.0,
    max_dte: int = 4,
) -> Optional[AlertEvent]:
    """Detect significant spot divergence from Max Pain near weekly expiry.

    [TRADING IMPLICATION]: When spot is far from Max Pain with <= 4 days
    to expiry, structural forces (dealer hedging, pin risk) create pressure
    toward Max Pain. Spot ABOVE Max Pain = put pressure (favorable for puts).
    Spot BELOW Max Pain = call pressure (favorable for calls). The signal
    weight increases as expiry approaches — Wednesday/Thursday is the
    prime window for weekly options.

    Args:
        spot: Current SPX spot price.
        max_pain: MaxPainResult from oi_engine.
        days_to_expiry: Calendar days until Friday expiry.
        divergence_points: Minimum distance in points to trigger. Default 80.
        max_dte: Maximum days to expiry for the alert to be active. Default 4.

    Returns:
        AlertEvent if conditions met, None otherwise.
    """
    if spot <= 0:
        log.debug("MAXPAIN_DIVERGENCE: invalid spot %.2f — skipping", spot)
        return None

    if max_pain is None:
        log.debug("MAXPAIN_DIVERGENCE: no Max Pain data — skipping")
        return None

    if days_to_expiry < 0:
        log.debug("MAXPAIN_DIVERGENCE: negative DTE %d — skipping (past expiry)", days_to_expiry)
        return None

    # Guard: only active within max_dte window
    if days_to_expiry > max_dte:
        log.debug(
            "MAXPAIN_DIVERGENCE: DTE %d > max_dte %d — too far from expiry",
            days_to_expiry,
            max_dte,
        )
        return None

    distance = spot - max_pain.strike
    abs_distance = abs(distance)

    if abs_distance < divergence_points:
        return None

    # Direction and implication
    if distance > 0:
        direction = "ABOVE"
        pressure = "bajista"
        favorable_for = "PUTS"
        interpretation = (
            f"Spot {abs_distance:.0f}pts POR ENCIMA de Max Pain -- "
            f"presion estructural {pressure} hacia {max_pain.strike:.0f}. "
            f"Favorable para {favorable_for}."
        )
    else:
        direction = "BELOW"
        pressure = "alcista"
        favorable_for = "CALLS"
        interpretation = (
            f"Spot {abs_distance:.0f}pts POR DEBAJO de Max Pain -- "
            f"presion estructural {pressure} hacia {max_pain.strike:.0f}. "
            f"Favorable para {favorable_for}."
        )

    # Severity scales with proximity to expiry
    if days_to_expiry <= 1:
        severity = "CRITICAL"
        urgency = "DIA DE VENCIMIENTO"
    elif days_to_expiry <= 2:
        severity = "WARNING"
        urgency = "Jueves -- ventana optima"
    elif days_to_expiry <= 3:
        severity = "WARNING"
        urgency = "Miercoles -- entrando en ventana optima"
    else:
        severity = "INFO"
        urgency = f"{days_to_expiry} dias para vencimiento"

    message = (
        f"Divergencia Max Pain: {interpretation} "
        f"DTE: {days_to_expiry}d ({urgency}). "
        f"Max Pain: {max_pain.strike:.0f} | Spot: {spot:.2f} | "
        f"Distancia: {abs_distance:.0f}pts ({max_pain.distance_pct:+.2f}%)."
    )

    log.info(
        "MAXPAIN_DIVERGENCE triggered: spot=%.2f, max_pain=%.0f, distance=%.0f, DTE=%d",
        spot,
        max_pain.strike,
        abs_distance,
        days_to_expiry,
    )

    return AlertEvent(
        alert_type="MAXPAIN_DIVERGENCE",
        severity=severity,
        title=f"Divergencia Max Pain: Spot {'ENCIMA' if direction == 'ABOVE' else 'DEBAJO'} por {abs_distance:.0f}pts",
        message=message,
        payload={
            "spot": spot,
            "max_pain_strike": max_pain.strike,
            "distance": round(distance, 2),
            "abs_distance": round(abs_distance, 2),
            "direction": direction,
            "pressure": pressure,
            "favorable_for": favorable_for,
            "days_to_expiry": days_to_expiry,
            "urgency": urgency,
            "distance_pct": max_pain.distance_pct,
            "divergence_threshold": divergence_points,
        },
    )


# ---------------------------------------------------------------------------
# 5. OI_BUILDUP
# ---------------------------------------------------------------------------


def check_oi_buildup(
    current_concentration: list[OIConcentration],
    previous_oi_map: dict[float, dict],
    buildup_pct: float = 20.0,
    min_oi_increase: int = 1000,
) -> list[AlertEvent]:
    """Detect significant OI buildup at individual strikes vs prior snapshot.

    [TRADING IMPLICATION]: A >20% OI increase with >= 1000 new contracts
    at a strike signals institutional positioning. Call buildup near ATM
    = potential upside bet or covered call selling. Put buildup = hedging
    or outright downside positioning. Context matters — the alert provides
    the data, the trader interprets.

    Args:
        current_concentration: Current OI concentration list from oi_engine.
        previous_oi_map: Prior day's OI by strike. Format:
            {strike: {"call_oi": int, "put_oi": int}}.
        buildup_pct: Minimum percentage increase to trigger. Default 20.0.
        min_oi_increase: Minimum absolute OI increase in contracts to
            filter noise. Default 1000.

    Returns:
        List of AlertEvents (one per strike with significant buildup).
        May be empty.
    """
    if not current_concentration:
        log.debug("OI_BUILDUP: no current OI concentration data — skipping")
        return []

    if not previous_oi_map:
        log.debug("OI_BUILDUP: no previous OI map — skipping (need baseline)")
        return []

    alerts: list[AlertEvent] = []

    for oi_entry in current_concentration:
        strike = oi_entry.strike

        prev = previous_oi_map.get(strike)
        if prev is None:
            # Strike didn't exist yesterday — could be new positioning
            # but without a baseline we can't calculate % change reliably
            # FUTURE EXTENSION: detect entirely new strike positions
            continue

        prev_call_oi = prev.get("call_oi", 0)
        prev_put_oi = prev.get("put_oi", 0)
        prev_total = prev_call_oi + prev_put_oi

        if prev_total <= 0:
            # Previous was zero — can't compute meaningful percentage
            continue

        current_total = oi_entry.total_oi
        total_change = current_total - prev_total
        total_change_pct = (total_change / prev_total) * 100.0

        # Check call-side buildup independently
        call_change = oi_entry.call_oi - prev_call_oi
        call_change_pct = (call_change / prev_call_oi * 100.0) if prev_call_oi > 0 else 0.0

        # Check put-side buildup independently
        put_change = oi_entry.put_oi - prev_put_oi
        put_change_pct = (put_change / prev_put_oi * 100.0) if prev_put_oi > 0 else 0.0

        # Determine which side drove the buildup
        buildup_events: list[dict] = []

        if call_change_pct >= buildup_pct and call_change >= min_oi_increase:
            buildup_events.append({
                "side": "CALL",
                "change": call_change,
                "change_pct": round(call_change_pct, 1),
                "current_oi": oi_entry.call_oi,
                "previous_oi": prev_call_oi,
            })

        if put_change_pct >= buildup_pct and put_change >= min_oi_increase:
            buildup_events.append({
                "side": "PUT",
                "change": put_change,
                "change_pct": round(put_change_pct, 1),
                "current_oi": oi_entry.put_oi,
                "previous_oi": prev_put_oi,
            })

        # Also check total buildup (catches cases where both sides grew)
        if (
            total_change_pct >= buildup_pct
            and total_change >= min_oi_increase
            and not buildup_events
        ):
            buildup_events.append({
                "side": "TOTAL",
                "change": total_change,
                "change_pct": round(total_change_pct, 1),
                "current_oi": current_total,
                "previous_oi": prev_total,
            })

        for event in buildup_events:
            side = event["side"]

            if side == "CALL":
                implication = "Posicionamiento alcista o venta de calls -- vigilar seguimiento direccional."
            elif side == "PUT":
                implication = "Cobertura bajista o apuesta direccional -- proteccion institucional o conviccion."
            else:
                implication = "Aumento amplio de posiciones -- ambos lados activos."

            severity = "WARNING" if event["change_pct"] >= 50.0 else "INFO"

            message = (
                f"Acumulacion de OI en {strike:.0f}: {side} OI +{event['change']:,} contratos "
                f"(+{event['change_pct']:.1f}%). "
                f"Actual: {event['current_oi']:,} | Anterior: {event['previous_oi']:,}. "
                f"{implication}"
            )

            log.info(
                "OI_BUILDUP triggered: strike=%.0f, side=%s, change=%+d (%.1f%%)",
                strike,
                side,
                event["change"],
                event["change_pct"],
            )

            alerts.append(AlertEvent(
                alert_type="OI_BUILDUP",
                severity=severity,
                title=f"Acumulacion OI: {side} en {strike:.0f} (+{event['change_pct']:.0f}%)",
                message=message,
                payload={
                    "strike": strike,
                    "side": side,
                    "oi_change": event["change"],
                    "oi_change_pct": event["change_pct"],
                    "current_oi": event["current_oi"],
                    "previous_oi": event["previous_oi"],
                    "total_oi_current": current_total,
                    "total_oi_previous": prev_total,
                    "call_oi": oi_entry.call_oi,
                    "put_oi": oi_entry.put_oi,
                    "buildup_pct_threshold": buildup_pct,
                    "min_oi_increase_threshold": min_oi_increase,
                },
            ))

    return alerts


# ---------------------------------------------------------------------------
# Alert Registry — extensible without if/else chains
# ---------------------------------------------------------------------------

# Each entry: alert_type -> function reference
# New alerts register themselves here. The scheduler never needs to know
# about individual alert implementations.
ALERT_REGISTRY: dict[str, AlertFunction] = {
    "GEX_FLIP_NEGATIVE": check_gex_flip_negative,
    "SPOT_OI_PROXIMITY": check_spot_oi_proximity,
    "VIX_LEVEL": check_vix_level,
    "MAXPAIN_DIVERGENCE": check_maxpain_divergence,
    "OI_BUILDUP": check_oi_buildup,
}


# ---------------------------------------------------------------------------
# Orchestrator — single entry point for the scheduler
# ---------------------------------------------------------------------------


def evaluate_all_alerts(
    spot: float,
    vix: float,
    previous_vix: float,
    gex: Optional[GEXResult],
    previous_gex: Optional[float],
    max_pain: Optional[MaxPainResult],
    days_to_expiry: int,
    oi_concentration: list[OIConcentration],
    previous_oi_map: dict[float, dict],
    proximity_points: float = 30.0,
    top_n_strikes: int = 3,
    divergence_points: float = 80.0,
    max_dte: int = 4,
    vix_thresholds: Optional[list[tuple[float, str]]] = None,
    buildup_pct: float = 20.0,
    min_oi_increase: int = 1000,
    disabled_alerts: Optional[set[str]] = None,
) -> list[AlertEvent]:
    """Evaluate all registered alerts and return triggered events.

    This is the single function the scheduler calls each cycle. It
    dispatches to each alert function with the appropriate inputs and
    collects results. Alerts can be selectively disabled via the
    disabled_alerts parameter.

    Args:
        spot: Current SPX spot price.
        vix: Current VIX value.
        previous_vix: VIX from the prior evaluation cycle.
        gex: Current GEX result from oi_engine (may be None).
        previous_gex: Net GEX from the prior cycle (may be None).
        max_pain: Current Max Pain result from oi_engine (may be None).
        days_to_expiry: Calendar days to Friday weekly expiry.
        oi_concentration: Ranked OI concentration list from oi_engine.
        previous_oi_map: Prior day OI map {strike: {call_oi, put_oi}}.
        proximity_points: Distance threshold for SPOT_OI_PROXIMITY.
        top_n_strikes: Number of top OI strikes to monitor.
        divergence_points: Distance threshold for MAXPAIN_DIVERGENCE.
        max_dte: Max DTE window for MAXPAIN_DIVERGENCE.
        vix_thresholds: Custom VIX thresholds [(level, severity), ...].
        buildup_pct: Percentage threshold for OI_BUILDUP.
        min_oi_increase: Minimum absolute OI change for OI_BUILDUP.
        disabled_alerts: Set of alert_type strings to skip this cycle.

    Returns:
        List of all triggered AlertEvents across all alert types.
    """
    if disabled_alerts is None:
        disabled_alerts = set()

    all_events: list[AlertEvent] = []

    # --- GEX_FLIP_NEGATIVE ---
    if "GEX_FLIP_NEGATIVE" not in disabled_alerts:
        try:
            event = check_gex_flip_negative(gex=gex, previous_gex=previous_gex)
            if event is not None:
                all_events.append(event)
        except Exception:
            log.exception("Error evaluating GEX_FLIP_NEGATIVE")

    # --- SPOT_OI_PROXIMITY ---
    if "SPOT_OI_PROXIMITY" not in disabled_alerts:
        try:
            events = check_spot_oi_proximity(
                spot=spot,
                oi_concentration=oi_concentration,
                proximity_points=proximity_points,
                top_n_strikes=top_n_strikes,
            )
            all_events.extend(events)
        except Exception:
            log.exception("Error evaluating SPOT_OI_PROXIMITY")

    # --- VIX_LEVEL ---
    if "VIX_LEVEL" not in disabled_alerts:
        try:
            events = check_vix_level(
                vix=vix,
                previous_vix=previous_vix,
                thresholds=vix_thresholds,
            )
            all_events.extend(events)
        except Exception:
            log.exception("Error evaluating VIX_LEVEL")

    # --- MAXPAIN_DIVERGENCE ---
    if "MAXPAIN_DIVERGENCE" not in disabled_alerts:
        try:
            event = check_maxpain_divergence(
                spot=spot,
                max_pain=max_pain,
                days_to_expiry=days_to_expiry,
                divergence_points=divergence_points,
                max_dte=max_dte,
            )
            if event is not None:
                all_events.append(event)
        except Exception:
            log.exception("Error evaluating MAXPAIN_DIVERGENCE")

    # --- OI_BUILDUP ---
    if "OI_BUILDUP" not in disabled_alerts:
        try:
            events = check_oi_buildup(
                current_concentration=oi_concentration,
                previous_oi_map=previous_oi_map,
                buildup_pct=buildup_pct,
                min_oi_increase=min_oi_increase,
            )
            all_events.extend(events)
        except Exception:
            log.exception("Error evaluating OI_BUILDUP")

    log.info(
        "Alert evaluation complete: %d event(s) triggered out of %d active rules",
        len(all_events),
        len(ALERT_REGISTRY) - len(disabled_alerts),
    )

    return all_events
