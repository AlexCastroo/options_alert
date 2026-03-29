"""
test_telegram_alerts.py -- Send one sample alert of each type to Telegram.

Usage (from container or local with .env loaded):
    python test_telegram_alerts.py

Sends 5 alerts + 1 startup message to verify formatting.
"""

import logging
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from src.alert_rules import AlertEvent
from src.gateways.telegram import send_alert, send_startup_message

DELAY = 2  # seconds between messages to avoid Telegram rate limits


def main() -> None:
    log = logging.getLogger("test_alerts")

    # 0. Startup message
    log.info("Sending startup message...")
    send_startup_message(spot=5782.30, vix=28.45)
    time.sleep(DELAY)

    # 1. GEX_FLIP_NEGATIVE — CRITICAL
    log.info("Sending GEX_FLIP_NEGATIVE...")
    send_alert(AlertEvent(
        alert_type="GEX_FLIP_NEGATIVE",
        severity="CRITICAL",
        title="Cambio de Regimen GEX: NEGATIVO",
        message=(
            "GEX paso a NEGATIVO -- dealers ahora amplifican movimientos. "
            "GEX Neto: -37.2M. Strike de flip: 5800. "
            "GEX anterior: +12.4M. Regimen: lateral -> TENDENCIAL."
        ),
        payload={
            "net_gex": -37_200_000,
            "net_gex_millions": -37.2,
            "previous_gex": 12_400_000,
            "previous_gex_millions": 12.4,
            "flip_strike": 5800,
            "call_gex": 15_056_649,
            "put_gex": -52_256_649,
            "regime": "NEGATIVO",
            "spot": 5782.30,
        },
        triggered_at=datetime.now(timezone.utc),
    ))
    time.sleep(DELAY)

    # 2. SPOT_OI_PROXIMITY — WARNING
    log.info("Sending SPOT_OI_PROXIMITY...")
    send_alert(AlertEvent(
        alert_type="SPOT_OI_PROXIMITY",
        severity="WARNING",
        title="Spot Cerca de Muro Put: 5750",
        message=(
            "Spot a 12pts del Muro Put en 5750 (OI: 45,230). "
            "Acercandose desde arriba -- soporte/iman -- "
            "dealers cubriendo puts generan presion compradora."
        ),
        payload={
            "spot": 5762.50,
            "strike": 5750,
            "distance": 12.50,
            "abs_distance": 12.50,
            "direction": "ABOVE",
            "call_oi": 8_200,
            "put_oi": 37_030,
            "total_oi": 45_230,
            "pct_of_total": 8.4,
            "dominance": "PUT",
            "proximity_points": 30.0,
        },
        triggered_at=datetime.now(timezone.utc),
    ))
    time.sleep(DELAY)

    # 3. VIX_LEVEL — CRITICAL
    log.info("Sending VIX_LEVEL...")
    send_alert(AlertEvent(
        alert_type="VIX_LEVEL",
        severity="CRITICAL",
        title="VIX Cruzo 30",
        message=(
            "VIX cruzo 30 (ahora 31.2). Anterior: 28.9. "
            "Miedo elevado en el mercado -- entorno de primas altas."
        ),
        payload={
            "vix": 31.20,
            "previous_vix": 28.90,
            "threshold": 30,
            "severity": "CRITICAL",
        },
        triggered_at=datetime.now(timezone.utc),
    ))
    time.sleep(DELAY)

    # 4. MAXPAIN_DIVERGENCE — WARNING
    log.info("Sending MAXPAIN_DIVERGENCE...")
    send_alert(AlertEvent(
        alert_type="MAXPAIN_DIVERGENCE",
        severity="WARNING",
        title="Divergencia Max Pain: Spot DEBAJO por 145pts",
        message=(
            "Divergencia Max Pain: Spot 145pts POR DEBAJO de Max Pain -- "
            "presion estructural alcista hacia 5900. Favorable para CALLS. "
            "DTE: 2d (Jueves -- ventana optima). "
            "Max Pain: 5900 | Spot: 5755.00 | Distancia: 145pts (+2.52%)."
        ),
        payload={
            "spot": 5755.00,
            "max_pain_strike": 5900,
            "distance": -145.00,
            "abs_distance": 145.00,
            "direction": "BELOW",
            "pressure": "alcista",
            "favorable_for": "CALLS",
            "days_to_expiry": 2,
            "urgency": "Jueves -- ventana optima",
            "distance_pct": -2.52,
            "divergence_threshold": 80.0,
        },
        triggered_at=datetime.now(timezone.utc),
    ))
    time.sleep(DELAY)

    # 5. OI_BUILDUP — INFO
    log.info("Sending OI_BUILDUP...")
    send_alert(AlertEvent(
        alert_type="OI_BUILDUP",
        severity="INFO",
        title="Acumulacion OI: PUT en 5700 (+34%)",
        message=(
            "Acumulacion de OI en 5700: PUT OI +4,520 contratos (+34.2%). "
            "Actual: 17,730 | Anterior: 13,210. "
            "Cobertura bajista o apuesta direccional -- "
            "proteccion institucional o conviccion."
        ),
        payload={
            "strike": 5700,
            "side": "PUT",
            "oi_change": 4520,
            "oi_change_pct": 34.2,
            "current_oi": 17730,
            "previous_oi": 13210,
            "total_oi_current": 24500,
            "total_oi_previous": 19980,
            "call_oi": 6770,
            "put_oi": 17730,
            "buildup_pct_threshold": 20.0,
            "min_oi_increase_threshold": 1000,
        },
        triggered_at=datetime.now(timezone.utc),
    ))

    log.info("Las 5 alertas de prueba + mensaje de inicio enviados!")


if __name__ == "__main__":
    main()
