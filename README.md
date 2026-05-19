# schwab-bot

Automated options premium harvesting bot (covered calls + cash-secured puts) running on AWS Lightsail.

**Strategy:** `premium_harvest_v1`  
**Watchlist:** SCHB, SPY, QQQ, IWM, GLD, TLT  
**Cycle:** every 5 minutes during market hours  
**Mode:** configurable via `BOT_MODE` env var (`paper` | `live`)

## Dashboard

Served at `/dashboard/` on the bot's Flask port.

### Panels
- **Top stats bar** — Mode, Kill Switch, Uptime, Cycles Run, Orders Placed, Premium Collected
- **Bot Brain panel** — live view of what the bot is doing right now: current state, strategy loaded, market status (OPEN/CLOSED), countdown to next market event, countdown to next cycle, last cycle reasoning (medium verbosity), watchlist & parameters
- **Live Event Feed** — SSE stream of every decision event
- **Account Positions** — buying power, equity & option position counts

### API Routes
| Route | Description |
|---|---|
| `GET /dashboard/` | Dashboard HTML UI |
| `GET /dashboard/api/status` | Bot status snapshot |
| `GET /dashboard/api/brain` | Bot Brain state (mode, strategy, market status, countdowns, reasoning) |
| `GET /dashboard/api/events` | Recent decision events |
| `GET /dashboard/api/stats` | Aggregate stats |
| `GET /dashboard/api/positions` | Current positions |
| `GET /dashboard/stream` | SSE live event stream |
| `POST /dashboard/kill` | Engage kill switch |
| `POST /dashboard/resume` | Disengage kill switch |

## Files

| File | Purpose |
|---|---|
| `app/bot.py` | Main trading loop + `_bot_state` dict (written each cycle, never changes logic) |
| `app/dashboard.py` | Flask Blueprint — all dashboard routes including `/api/brain` |
| `app/templates/dashboard.html` | Single-file dashboard UI with Bot Brain panel |

## Deployment

Runs as a `systemd` service on AWS Lightsail (`Project_Alpha_Crash_Report`, `3.146.163.158`).  
Backups of pre-patch files are kept as `.bak` on the server.

```
sudo systemctl restart schwab-bot
sudo systemctl status schwab-bot
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BOT_MODE` | `paper` | `paper` or `live` |
| `BOT_STRATEGY` | `premium_harvest_v1` | Strategy identifier |
| `BOT_WATCHLIST` | `SPY,QQQ,IWM,GLD,TLT` | Comma-separated symbols |
| `CYCLE_INTERVAL_SECONDS` | `300` | Seconds between cycles |
| `MIN_PREMIUM` | `0.50` | Minimum option premium ($) |
| `MAX_OPEN_POSITIONS` | `10` | Maximum concurrent open positions |
| `TARGET_DELTA` | `0.30` | Target delta for strike selection |
| `TARGET_DTE` | `30` | Target days-to-expiration |
| `PROFIT_TAKE_PCT` | `0.50` | Close at 50% profit |
