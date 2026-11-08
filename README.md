# schwab-bot

Automated options premium harvesting bot (covered calls + cash-secured puts) running on AWS Lightsail.

**Strategy:** `premium_harvest_v1`  
**Watchlist default in `app/bot.py`:** `SPY,QQQ,IWM,GLD,TLT` (no SCHB). See "Defaults that disagree" below.  
**Cycle:** every 5 minutes (`CYCLE_INTERVAL_SECONDS`, default 300) during the market-hours check in `app/bot.py`  
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
| `GET /dashboard/api/positions` | Current positions (`schwab_account.SchwabAccount`) |
| `GET /dashboard/api/account` | Buying power plus equity and option counts |
| `GET /dashboard/stream` | SSE live event stream (heartbeat every 15s) |
| `POST /dashboard/kill` | Engage kill switch (`risk.guards.enable_kill_switch`) |
| `POST /dashboard/resume` | Disengage kill switch |

`/api/brain` JSON fields: `mode`, `strategy`, `current_action`, `last_reasoning` (the loop keeps the last 2 lines), `kill_switch_active`, `market_open`, `seconds_until_next_event`, `next_event` (`open` or `close`), `server_utc`, `next_cycle_at`, `secs_until_cycle`, `cycle_interval_seconds`, `watchlist`, `min_premium`, `max_open`.

`POST /kill` and `POST /resume` return HTTP 500 with `{ok: false, error}` when `risk.guards` or `app.event_log` cannot be imported. `/api/events`, `/api/stats`, and `/stream` call `app.event_log` with no fallback.

## Files

| File | Purpose |
|---|---|
| `app/bot.py` | Main trading loop + `_bot_state` dict (written each cycle, never changes logic) |
| `app/dashboard.py` | Flask Blueprint — all dashboard routes including `/api/brain` |
| `app/templates/dashboard.html` | Single-file dashboard UI with Bot Brain panel |

## What this checkout can run

`app/bot.py` imports `strategies.premium_harvest`, `risk.guards`, `schwab_account`, `schwab_market_data`, `schwab_orders`, and `app.event_log` at import time. None of those modules are in this repository. Importing `app.bot` here fails before a cycle starts.

`GET /dashboard/api/brain` catches that import error and still returns JSON: `current_action` is `unknown`, and `last_reasoning` is the exception text. `GET /dashboard/api/status` still returns 200. It sets `kill_switch_active` to `null` when `risk.guards` is missing. The `app.bot` import error is not copied into the JSON; `cycle_count`, `uptime_seconds`, and `last_cycle_ts` are then null. The HTML page can load because the template is read from disk at import.

## Deployment

This repository has no systemd unit and no deploy workflow. Do not restart a service on `3.146.163.158` from this README. That address is the Market Fragility host.

The Design-Project monorepo workflow `.github/workflows/deploy-schwab-bot.yml` deploys `projects/trading-bot/` and checks `http://localhost:8001/dashboard/` and `http://localhost:8001/dashboard/api/brain`. The host secret is `SCHWAB_BOT_LIGHTSAIL_HOST`, documented as `3.18.195.165` (`trading-app`). `docs/LIGHTSAIL_SERVICES.md` in that repo is the host map. The unit name recorded there is `schwab-bot.service`.

## Defaults that disagree

| Surface | Watchlist when `BOT_WATCHLIST` is unset |
|---|---|
| `app/bot.py` (`WATCHLIST`, and `/api/brain` when the import works) | `SPY,QQQ,IWM,GLD,TLT` |
| `GET /dashboard/api/status` | `SCHB,SPY,QQQ,IWM,GLD,TLT` |
| `GET /dashboard/api/brain` when `app.bot` fails to import | `SCHB,SPY,QQQ` |

Set `BOT_WATCHLIST` explicitly. The header of an older copy of this README listed SCHB in the live watchlist; that matches only the status-route default.

## Market clock

`app/bot.py` and `/api/brain` both shift UTC by a fixed −5 hours (EST) all year. During EDT, 09:30 and 16:00 on that clock are 10:30 and 17:00 Eastern, so the open/close countdowns are one hour early. The weekday also flips at 05:00 UTC instead of midnight Eastern.

The two checks also disagree at the closing minute, using that same shifted clock:

- `bot._market_is_open` treats 16:00 as open (`<= 16:00`).
- `/api/brain` treats 16:00 as closed (`< 16:00`) and counts down to the next 09:30.

The brain helper's comment says the frontend displays CDT. The server math is still the fixed −5 hour shift. Weekend is Saturday and Sunday on that shifted clock. Neither function calls Schwab market hours.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BOT_MODE` | `paper` | `paper` or `live` |
| `BOT_STRATEGY` | `premium_harvest_v1` | Strategy identifier |
| `BOT_WATCHLIST` | `SPY,QQQ,IWM,GLD,TLT` in `app/bot.py` | Comma-separated symbols. `/api/status` uses a different default; see above. |
| `CYCLE_INTERVAL_SECONDS` | `300` | Seconds between cycles |
| `MIN_PREMIUM` | `0.50` | Minimum option premium ($) |
| `MAX_OPEN_POSITIONS` | `10` | Maximum concurrent open positions |
| `TARGET_DELTA` | `0.30` | Target delta for strike selection |
| `TARGET_DTE` | `30` | Target days-to-expiration |
| `PROFIT_TAKE_PCT` | `0.50` | Close at 50% profit |
