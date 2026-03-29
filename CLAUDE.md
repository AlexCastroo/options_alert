# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SPX weekly options alert system. Monitors SPX spot, VIX, and Open Interest chains to fire configurable alerts via Telegram. MVP stack: Python 3.11 + yfinance + SQLite. Phase 2 will replace yfinance polling with Polygon.io WebSocket.

## Commands

### Local Development
```bash
python -m venv venv
source venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
python main.py                  # Smoke test: validates SPX/VIX data feeds
```

### Docker
```bash
docker-compose build
docker-compose up
docker-compose up -d            # Detached
docker-compose logs -f          # Tail logs
```

### Environment Setup Sequence (new environment)
1. Verify Python 3.10+
2. Create/activate venv at `./venv`
3. `pip install -r requirements.txt`
4. Copy `.env.example` → `.env`, fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
5. Initialize SQLite schema (once `state_manager.py` exists)
6. Smoke test: fetch SPX spot and VIX via yfinance
7. Smoke test: fetch nearest weekly expiry OI chain for SPX

## Architecture

### Data Flow
```
Scheduler (60s poll, UTC 13:00–21:00)
  → market_data.py   — fetch SPX spot, VIX, OI chain (yfinance MVP; Polygon.io Phase 2)
  → oi_engine.py     — Max Pain, GEX, P/C ratio, OI concentration
  → alert_rules.py   — registry-based trigger evaluation (not if/else)
  → state_manager.py — dedup check (15-min cooldown), persist to SQLite, update snapshots
  → telegram.py      — send enriched alert with market context
```

### Planned Module Map (`src/`)
| Module | Responsibility |
|--------|----------------|
| `market_data.py` | Data ingestion; plug-in interface for swapping sources |
| `alert_rules.py` | Pure trigger logic; modular registry, no global state |
| `state_manager.py` | SQLite CRUD, dedup, runtime config from `alert_config` table |
| `engines/oi_engine.py` | Max Pain, GEX, OI buildup calculations |
| `gateways/telegram.py` | Telegram Bot API notification channel |
| `scheduler.py` | Main polling loop, market-hours awareness |

### Five Alert Types
- `GEX_REGIME` — GEX crosses zero (trending regime shift)
- `SPOT_OI_PROXIMITY` — Spot within N points of high-OI strike
- `MAXPAIN_DIVERGENCE` — Spot far from Max Pain (Wednesday–Thursday weighted)
- `VIX_LEVEL` — VIX crosses configurable threshold (20/25/30)
- `OI_BUILDUP` — OI on any strike increases >20% vs prior day

### SQLite Schema (initialize in `state_manager.py`)
Tables: `market_snapshots`, `oi_snapshots`, `alert_log` (full JSON payload), `alert_config` (key/value runtime settings). Seed `alert_config` with defaults on init. **Schema changes must be additive only — never DROP or rename columns.**

### Config Files (create if missing)
- `config/assets.json` — assets to monitor (SPX, IBEX35, etc.)
- `config/settings.json` — non-secret global config (thresholds, intervals)
- `.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (never commit)

## Coding Standards

- Type hints on every function signature; docstrings on all public functions
- Dataclasses for result/event objects
- `logging` module only — never `print()`
- Guard clauses at function entry
- No hardcoded credentials, thresholds, or asset symbols — all from `alert_config` or config files
- Mark any decision with trading implications: `# [TRADING IMPLICATION]: <rationale>`
- Mark future extension points: `# FUTURE EXTENSION: <description>`

## Key Constraints

- Alert dedup window: 15 minutes (configurable via `alert_config`)
- Polling interval: 60 seconds (configurable)
- Market hours: UTC 13:00–21:00
- Memory limit in Docker: 256 MB
- Integration tests must hit a real SQLite instance — no mocks for the database layer
