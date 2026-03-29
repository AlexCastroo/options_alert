"""
telegram.py — Telegram Bot API gateway for the Options Alert System.

Sends formatted alert messages via the Telegram Bot API. This is the
primary notification channel. Additional channels (WhatsApp/Twilio, email)
will be added as separate gateway modules.

FUTURE EXTENSION: WhatsApp via Twilio gateway (same AlertEvent contract)
FUTURE EXTENSION: Email gateway for daily summary digests
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

from src.alert_rules import AlertEvent

log = logging.getLogger("options_alert.gateways.telegram")

# ---------------------------------------------------------------------------
# Telegram Bot API config
# ---------------------------------------------------------------------------
_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT_SECONDS = 10

# Severity emoji mapping
_SEVERITY_EMOJI: dict[str, str] = {
    "CRITICAL": "\U0001f534",  # Red circle
    "WARNING": "\U0001f7e1",   # Yellow circle
    "INFO": "\U0001f535",      # Blue circle
}

# Alert type display config: (emoji, human-readable label)
_ALERT_TYPE_DISPLAY: dict[str, tuple[str, str]] = {
    "GEX_FLIP_NEGATIVE": ("\U0001f300", "Cambio de Regimen GEX"),        # Cyclone
    "SPOT_OI_PROXIMITY": ("\U0001f3af", "Spot Cerca de Muro OI"),       # Target
    "VIX_LEVEL": ("\U0001f4c8", "VIX Cruza Umbral"),                    # Chart increasing
    "MAXPAIN_DIVERGENCE": ("\U0001f9f2", "Divergencia Max Pain"),       # Magnet
    "OI_BUILDUP": ("\U0001f4ca", "Acumulacion de OI"),                  # Bar chart
}


def _get_credentials() -> tuple[Optional[str], Optional[str]]:
    """Read Telegram credentials from environment.

    Returns:
        Tuple of (bot_token, chat_id). Either may be None if not configured.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    return token, chat_id


def _escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 format.

    Telegram MarkdownV2 requires escaping these characters:
    _ * [ ] ( ) ~ ` > # + - = | { } . !

    Args:
        text: Raw text to escape.

    Returns:
        Escaped text safe for MarkdownV2.
    """
    # Characters that must be escaped in MarkdownV2
    special_chars = r"_*[]()~`>#+=|{}.!-"
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += f"\\{char}"
        else:
            escaped += char
    return escaped


def _format_alert_message(event: AlertEvent) -> str:
    """Format an AlertEvent into a rich Telegram MarkdownV2 message.

    Layout:
        [type_emoji] *Alert Title*  [severity_emoji] SEVERITY
        ━━━━━━━━━━━━━━━━━━━━━━━━
        [message body - trader language]

        📊 Key Metrics:
        ├ metric_1: value
        ├ metric_2: value
        └ metric_3: value

        ⏱ 2026-03-29 15:41:00 UTC

    Args:
        event: The AlertEvent to format.

    Returns:
        MarkdownV2-formatted message string.
    """
    esc = _escape_markdown_v2
    p = event.payload

    severity_emoji = _SEVERITY_EMOJI.get(event.severity, "\U0001f535")
    type_emoji, type_label = _ALERT_TYPE_DISPLAY.get(
        event.alert_type, ("\U0001f514", event.alert_type)
    )

    # Header: type emoji + bold title + severity badge
    header = f"{type_emoji} *{esc(event.title)}*  {severity_emoji}"

    # Heavy separator
    sep = esc("━" * 26)

    # Body message
    body = esc(event.message)

    # Key metrics block — varies per alert type
    metrics_lines = _build_metrics_block(event)
    metrics_section = ""
    if metrics_lines:
        metrics_header = "\U0001f4ca *Metricas clave:*"
        formatted_lines = []
        for i, line in enumerate(metrics_lines):
            prefix = "\U0000251c" if i < len(metrics_lines) - 1 else "\U00002514"
            formatted_lines.append(f"{prefix} {esc(line)}")
        metrics_section = f"\n\n{metrics_header}\n" + "\n".join(formatted_lines)

    # Timestamp footer
    ts = event.triggered_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    footer = f"\U000023f1 {esc(ts)}"

    message = f"{header}\n{sep}\n\n{body}{metrics_section}\n\n{footer}"
    return message


def _build_metrics_block(event: AlertEvent) -> list[str]:
    """Build alert-type-specific key metrics lines.

    Args:
        event: The AlertEvent with payload data.

    Returns:
        List of human-readable metric strings (not yet escaped).
    """
    p = event.payload
    lines: list[str] = []

    if event.alert_type == "GEX_FLIP_NEGATIVE":
        lines.append(f"GEX Neto: {p.get('net_gex_millions', 0):+.1f}M")
        lines.append(f"GEX Anterior: {p.get('previous_gex_millions', 0):+.1f}M")
        lines.append(f"Strike de Flip: {p.get('flip_strike', 'N/A')}")
        lines.append(f"Spot: {p.get('spot', 0):,.2f}")
        lines.append(f"Regimen: {p.get('regime', 'N/A')}")

    elif event.alert_type == "SPOT_OI_PROXIMITY":
        lines.append(f"Strike: {p.get('strike', 0):,.0f}")
        lines.append(f"Distancia: {p.get('abs_distance', 0):.0f} pts ({p.get('direction', '')})")
        lines.append(f"OI Total: {p.get('total_oi', 0):,}")
        lines.append(f"OI Calls: {p.get('call_oi', 0):,} | OI Puts: {p.get('put_oi', 0):,}")
        lines.append(f"Dominancia: {p.get('dominance', 'N/A')}")

    elif event.alert_type == "VIX_LEVEL":
        lines.append(f"VIX: {p.get('vix', 0):.2f}")
        lines.append(f"Anterior: {p.get('previous_vix', 0):.2f}")
        lines.append(f"Umbral: {p.get('threshold', 0):.0f}")

    elif event.alert_type == "MAXPAIN_DIVERGENCE":
        lines.append(f"Spot: {p.get('spot', 0):,.2f}")
        lines.append(f"Max Pain: {p.get('max_pain_strike', 0):,.0f}")
        lines.append(f"Distancia: {p.get('abs_distance', 0):.0f} pts ({p.get('distance_pct', 0):+.2f}%)")
        lines.append(f"DTE: {p.get('days_to_expiry', '?')}d ({p.get('urgency', '')})")
        lines.append(f"Presion: {p.get('pressure', '')} -> {p.get('favorable_for', '')}")

    elif event.alert_type == "OI_BUILDUP":
        lines.append(f"Strike: {p.get('strike', 0):,.0f}")
        lines.append(f"Lado: {p.get('side', 'N/A')}")
        lines.append(f"Cambio: +{p.get('oi_change', 0):,} ({p.get('oi_change_pct', 0):+.1f}%)")
        lines.append(f"OI Actual: {p.get('current_oi', 0):,}")
        lines.append(f"OI Anterior: {p.get('previous_oi', 0):,}")

    return lines


def _send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "MarkdownV2",
) -> bool:
    """Send a single message via Telegram Bot API with one retry on timeout.

    Args:
        token: Telegram Bot API token.
        chat_id: Target chat/channel ID.
        text: Message text (already formatted for parse_mode).
        parse_mode: Telegram parse mode. Default "MarkdownV2".

    Returns:
        True if message was delivered successfully, False otherwise.
    """
    url = _TELEGRAM_API_BASE.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    for attempt in range(1, 3):  # Max 2 attempts (1 original + 1 retry)
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    log.info("Telegram message delivered (attempt %d)", attempt)
                    return True
                else:
                    log.error(
                        "Telegram API returned ok=false: %s",
                        result.get("description", "unknown error"),
                    )
                    return False

            # Non-200 status
            log.error(
                "Telegram API HTTP %d: %s",
                response.status_code,
                response.text[:200],
            )
            # Don't retry on client errors (4xx) — likely bad token/chat_id
            if 400 <= response.status_code < 500:
                return False

            # Retry on server errors (5xx)
            if attempt < 2:
                log.warning("Retrying Telegram send (attempt %d failed with %d)", attempt, response.status_code)
                continue
            return False

        except requests.Timeout:
            if attempt < 2:
                log.warning("Telegram request timed out (attempt %d) — retrying", attempt)
                continue
            log.error("Telegram request timed out on retry — giving up")
            return False

        except requests.RequestException as exc:
            log.error("Telegram request failed: %s", exc)
            return False

    return False


def send_alert(event: AlertEvent) -> bool:
    """Send a formatted alert notification via Telegram.

    Args:
        event: The AlertEvent to deliver.

    Returns:
        True if the message was delivered successfully, False otherwise.
        Never raises — all errors are logged and return False.
    """
    token, chat_id = _get_credentials()

    if not token or not chat_id:
        log.warning(
            "Telegram not configured (token=%s, chat_id=%s) — skipping alert delivery",
            "set" if token else "MISSING",
            "set" if chat_id else "MISSING",
        )
        return False

    try:
        message = _format_alert_message(event)
        return _send_telegram_message(token, chat_id, message)
    except Exception:
        log.exception("Unexpected error formatting/sending Telegram alert")
        return False


def send_startup_message(spot: Optional[float] = None, vix: Optional[float] = None) -> bool:
    """Send a system startup notification via Telegram.

    Includes current SPX spot and VIX if available.

    Args:
        spot: Current SPX spot price, or None if unavailable.
        vix: Current VIX value, or None if unavailable.

    Returns:
        True if delivered, False otherwise.
    """
    token, chat_id = _get_credentials()

    if not token or not chat_id:
        log.warning("Telegram not configured — skipping startup message")
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    esc = _escape_markdown_v2

    lines = [
        "\U00002705 *Sistema de Alertas Activo*",
        esc("━" * 26),
        "",
    ]

    if spot is not None:
        lines.append(f"\U0001f4b9 SPX Spot: *{esc(f'{spot:,.2f}')}*")
    else:
        lines.append(f"\U0001f4b9 SPX Spot: {esc('no disponible')}")

    if vix is not None:
        vix_emoji = "\U0001f534" if vix >= 25 else ("\U0001f7e1" if vix >= 20 else "\U0001f7e2")
        lines.append(f"{vix_emoji} VIX: *{esc(f'{vix:.2f}')}*")
    else:
        lines.append(f"\U0001f4c8 VIX: {esc('no disponible')}")

    lines.append("")
    lines.append(esc("━" * 26))
    lines.append(f"\U000023f1 {esc(now)}")

    message = "\n".join(lines)

    try:
        return _send_telegram_message(token, chat_id, message)
    except Exception:
        log.exception("Unexpected error sending Telegram startup message")
        return False
