import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/options_alert.log", mode="a"),
    ],
)
log = logging.getLogger("options_alert")


def main() -> None:
    """Entry point — validates environment, then launches the scheduler."""
    log.info("Options Alert System — initializing")
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(os.getenv("TELEGRAM_BOT_TOKEN")))
    log.info("TELEGRAM_CHAT_ID present:  %s", bool(os.getenv("TELEGRAM_CHAT_ID")))

    # Quick sanity check: can we reach Yahoo Finance?
    from src.market_data import fetch_price

    spot = fetch_price("^GSPC")
    vix = fetch_price("^VIX")
    log.info("SPX Spot: %s", spot)
    log.info("VIX:      %s", vix)

    if spot and vix:
        log.info("Market data feeds OK — starting scheduler")
    else:
        log.warning("One or more feeds returned no data — starting scheduler anyway")

    # Launch the scheduler (blocks forever)
    from src.scheduler import Scheduler

    scheduler = Scheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
