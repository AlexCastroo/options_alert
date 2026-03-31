# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dual-mode options alert system:
1. **SPX Index mode** — monitors SPX spot, VIX, and OI chains for 5 structural alerts
2. **Equity scan mode** — scans 85 US equity/ETF tickers for unusual deep-OTM OI across all expiries

MVP stack: Python 3.11 + yfinance + SQLite + Telegram. Phase 2 will replace yfinance polling with Polygon.io WebSocket.

## Commands

### Docker (primary deployment)
```bash
docker-compose build --no-cache  # Force full rebuild (required after code changes)
docker-compose up -d             # Detached
docker-compose logs -f           # Tail logs
docker-compose restart           # Restart without rebuild (picks up config changes)
```

### Reset database (start fresh — clears baselines and cooldowns)
```bash
docker-compose down
rm -f data/options_alert.db data/options_alert.db-shm data/options_alert.db-wal
docker-compose up -d
```

### Clear only cooldowns (keep OI snapshots)
```bash
sqlite3 data/options_alert.db "DELETE FROM alert_log;"
```

### Local Development
```bash
python -m venv venv
source venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
python main.py
```

### Environment Setup Sequence (new environment)
1. Verify Python 3.10+
2. Create/activate venv at `./venv`
3. `pip install -r requirements.txt`
4. Copy `.env.example` → `.env`, fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
5. Initialize SQLite schema — `state_manager.init_db()` runs automatically on startup
6. Smoke test: fetch SPX spot and VIX via yfinance

## Architecture

### Data Flow — SPX pipeline (every 60s during market hours)
```
Scheduler
  → market_data.fetch_price()         — SPX spot + VIX
  → market_data.fetch_options_chain() — nearest weekly expiry OI (5-min cache)
  → oi_engine.analyze_oi()            — Max Pain, GEX, OI concentration
  → state_manager (load prev state)   — previous GEX/VIX/OI BEFORE saving current
  → state_manager (save snapshots)    — persist current cycle to SQLite
  → alert_rules.evaluate_all_alerts() — evaluate 5 SPX alert rules
  → state_manager.was_recently_alerted() — dedup check (15-min cooldown)
  → telegram.send_alert()             — deliver with MarkdownV2 formatting
  → state_manager.record_alert()      — persist to alert_log
```

### Data Flow — Equity unusual OI scan (every 5 cycles = ~5 min)
```
Scheduler._run_equity_scan()
  → fetch_price(symbol)                    — current spot per ticker
  → fetch_all_expiries_chain(symbol)       — ALL expiries OI (30-min cache)
  → state_manager.get_previous_oi_map()   — prior day OI per expiry (BEFORE saving)
  → state_manager.get_oi_first_seen_map() — earliest date each strike had OI > 0
  → state_manager.save_oi_snapshot()      — persist today's OI per expiry
  → alert_rules.check_unusual_otm_oi()    — filter: OTM≥80% + OI≥30k + buildup≥20%
  → state_manager.was_recently_alerted()  — dedup check (24h cooldown for OI alerts)
  → telegram.send_alert()                 — table format with expiry breakdown
```

### Module Map (`src/`)
| Module | Responsibility |
|--------|----------------|
| `market_data.py` | Data ingestion; `fetch_options_chain()` for SPX, `fetch_all_expiries_chain()` for equities |
| `alert_rules.py` | Pure trigger logic; registry pattern, no I/O, no DB |
| `state_manager.py` | SQLite CRUD, dedup, snapshots, `get_oi_first_seen_map()` |
| `engines/oi_engine.py` | Max Pain, GEX, OI concentration calculations |
| `gateways/telegram.py` | Telegram Bot API; MarkdownV2 formatting; expiry table renderer |
| `scheduler.py` | SPX loop + equity scan loop; asset config loader |

### Six Alert Types
| Alert | Trigger | Cooldown |
|-------|---------|---------|
| `GEX_FLIP_NEGATIVE` | GEX crosses from positive to negative | 15 min |
| `SPOT_OI_PROXIMITY` | Spot within 30pts of top-3 OI strike | 15 min |
| `MAXPAIN_DIVERGENCE` | Spot >80pts from Max Pain, DTE ≤ 4 | 15 min |
| `VIX_LEVEL` | VIX crosses 20/25/30 upward | 15 min |
| `OI_BUILDUP` | OI on any SPX strike increases >20% vs prior day | 15 min |
| `UNUSUAL_OTM_OI` | Equity OI >30k at strike ≥80% OTM, with ≥20% daily buildup | **24h** |

### UNUSUAL_OTM_OI — Design Decisions
- Fires once per day per (symbol, side) — OI is a daily figure, not intraday
- Requires OI buildup ≥20% vs prior day to filter stale historical positions
- New strikes (no prior baseline) are always allowed through — they're inherently fresh
- `first_seen` date tracked via `get_oi_first_seen_map()` — shows how long position has existed
- Day 1: all positions show as "Hoy" (no baseline) — signal quality improves from day 2
- **Only evaluates current-year expiries** — filter applied in `check_unusual_otm_oi` before scanning strikes. Positions in 2027+ LEAPS are ignored entirely. This prevents mismatches where the alert fires from a multi-year position but the table appears empty.
- `vol_oi_ratio` (volume/OI) computed per hit and included in payload — freshness proxy (≥0.5 Fresca, ≥0.1 Activa, <0.1 Antigua). Not currently displayed in Telegram table.
- Known limitation: OI buildup still doesn't confirm intraday freshness — volume cross-check is the next planned improvement

### SQLite Schema
Tables: `oi_snapshots`, `oi_summary`, `alert_log`, `alert_config`
- `oi_snapshots` stores both SPX and equity OI (keyed by symbol + expiry_date + snapshot_date)
- `get_oi_first_seen_map()` uses MIN(snapshot_date) grouped by (expiry, strike) — single query
- **Schema changes must be additive only — never DROP or rename columns**

### Config Files
- `config/assets.json` — equity watchlist (85 tickers), OI thresholds, scan interval. **Mounted as Docker volume — edit without rebuild, just restart.**
- `.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (never commit)

### Key Architectural Rule
**Always load previous state from SQLite BEFORE saving the current cycle.**
If you save first and then read, `get_previous_gex()` / `get_previous_vix()` return the
current cycle's data and cross-detection (GEX flip, VIX threshold) never fires.

## Coding Standards

- Type hints on every function signature; docstrings on all public functions
- Dataclasses for result/event objects (frozen=True where immutable)
- `logging` module only — never `print()`
- Guard clauses at function entry
- No hardcoded credentials, thresholds, or asset symbols — all from `alert_config` or config files
- Mark trading decisions: `# [TRADING IMPLICATION]: <rationale>`
- Mark extension points: `# FUTURE EXTENSION: <description>`

## Key Constraints

- SPX alert dedup: 15 minutes (via `alert_config`)
- UNUSUAL_OTM_OI dedup: 24 hours (hardcoded — OI is daily data)
- Equity scan interval: 5 cycles × 60s = ~5 minutes
- OI chain cache: 5 min (SPX), 30 min (equity all-expiries)
- Polling interval: 60 seconds
- Market hours: UTC 13:00–21:00
- Memory limit in Docker: 512 MB (increased from 256 MB — equity scan for 87 tickers peaks above 256 MB)
- Integration tests must hit a real SQLite instance — no mocks for the database layer
- SQLite persists on the host at `./data/options_alert.db` (volume `./data:/app/data`) — survives `docker-compose down` and rebuilds
