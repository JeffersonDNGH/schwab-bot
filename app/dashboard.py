"""dashboard.py
Flask Blueprint that serves the monitoring dashboard for the Schwab Options Bot.

Routes:
  GET  /dashboard/           - Serves the HTML dashboard UI
  GET  /dashboard/api/status - Bot status snapshot (JSON)
  GET  /dashboard/api/events - Recent events feed (JSON, ?limit=N)
  GET  /dashboard/api/stats  - Aggregate stats (JSON)
  GET  /dashboard/api/positions - Current option + equity positions (JSON)
  GET  /dashboard/api/brain  - Bot Brain state (JSON) ← NEW
  GET  /dashboard/stream     - Server-Sent Events stream for live updates
  POST /dashboard/kill       - Engage kill switch
  POST /dashboard/resume     - Disengage kill switch
"""
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from flask import Blueprint, Response, jsonify, render_template_string, request, stream_with_context

from app import event_log

log = logging.getLogger(__name__)

dashboard = Blueprint("dashboard", __name__, url_prefix="/dashboard")

# ---------------------------------------------------------------------------
# HTML Dashboard (single-file, no external file dependencies)
# ---------------------------------------------------------------------------
_DASHBOARD_HTML = open(
    os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
).read() if os.path.exists(
    os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
) else "<h1>Dashboard template not found</h1>"


@dashboard.get("/")
def dashboard_ui():
    """Serve the dashboard HTML page."""
    return _DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@dashboard.get("/api/status")
def api_status():
    """Current bot status snapshot."""
    try:
        from app.bot import get_status
        bot_status = get_status()
    except Exception as exc:
        bot_status = {"error": str(exc)}

    try:
        from risk.guards import is_kill_switch_active
        kill = is_kill_switch_active()
    except Exception:
        kill = None

    return jsonify({
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": os.getenv("BOT_MODE", "paper"),
        "strategy": os.getenv("BOT_STRATEGY", "premium_harvest_v1"),
        "watchlist": os.getenv("BOT_WATCHLIST", "SCHB,SPY,QQQ,IWM,GLD,TLT").split(","),
        "kill_switch_active": kill,
        "uptime_seconds": bot_status.get("uptime_seconds"),
        "cycle_count": bot_status.get("cycle_count"),
        "last_cycle_ts": bot_status.get("last_cycle_ts"),
        "paper_mode": os.getenv("BOT_MODE", "paper") == "paper",
    })


@dashboard.get("/api/events")
def api_events():
    """Recent decision events. ?limit=N (default 100, max 500)"""
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except ValueError:
        limit = 100
    events = event_log.get_recent(limit)
    return jsonify({"events": events, "count": len(events)})


@dashboard.get("/api/stats")
def api_stats():
    """Aggregate statistics from in-memory event buffer."""
    stats = event_log.get_stats()
    return jsonify(stats)


@dashboard.get("/api/positions")
def api_positions():
    """Current account positions from Schwab."""
    try:
        from schwab_account import SchwabAccount
        acct = SchwabAccount()
        equity = acct.get_equity_positions()
        options = acct.get_option_positions()
        buying_power = acct.get_buying_power()
        return jsonify({
            "equity_positions": equity,
            "option_positions": options,
            "buying_power": buying_power,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "equity_positions": [], "option_positions": [], "buying_power": None}), 200


@dashboard.get("/api/account")
def api_account():
    """Account summary: buying power, equity, option count."""
    try:
        from schwab_account import SchwabAccount
        acct = SchwabAccount()
        return jsonify({
            "buying_power": acct.get_buying_power(),
            "equity_positions_count": len(acct.get_equity_positions()),
            "option_positions_count": len(acct.get_option_positions()),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 200


# ---------------------------------------------------------------------------
# Bot Brain route — NEW (nothing above this line was modified)
# ---------------------------------------------------------------------------

def _market_open_status() -> dict:
    """Return market open/closed status + seconds until next open or close.
    Times in UTC; the frontend converts to CDT for display.
    """
    now_utc = datetime.now(timezone.utc)
    # ET is UTC-5 (use conservative EST offset — same logic as bot.py)
    et_offset = timedelta(hours=-5)
    now_et = now_utc + et_offset

    weekday = now_et.weekday()           # 0=Mon … 6=Sun
    et_minutes = now_et.hour * 60 + now_et.minute

    OPEN_MIN  = 9 * 60 + 30   # 09:30 ET
    CLOSE_MIN = 16 * 60        # 16:00 ET

    is_weekend = weekday >= 5
    is_open = (not is_weekend) and (OPEN_MIN <= et_minutes < CLOSE_MIN)

    # --- compute seconds until next event ---
    if is_open:
        # seconds until 16:00 ET today
        close_today_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        secs_until = int((close_today_et - now_et).total_seconds())
        next_event_label = "close"
    else:
        # Find next weekday open at 09:30 ET
        days_ahead = 0
        candidate = now_et
        while True:
            days_ahead += 1
            candidate = now_et + timedelta(days=days_ahead)
            if candidate.weekday() < 5:
                break
        next_open_et = candidate.replace(hour=9, minute=30, second=0, microsecond=0)
        secs_until = int((next_open_et - now_et).total_seconds())
        next_event_label = "open"

    return {
        "market_open": is_open,
        "seconds_until_next_event": max(secs_until, 0),
        "next_event": next_event_label,   # "open" | "close"
        "server_utc": now_utc.isoformat(),
    }


@dashboard.get("/api/brain")
def api_brain():
    """Bot Brain state — consumed by the new Brain panel on the dashboard."""
    # Market status
    mkt = _market_open_status()

    # Bot internal state
    try:
        from app.bot import get_brain_state, BOT_MODE, BOT_STRATEGY, WATCHLIST, CYCLE_INTERVAL
        from risk.guards import is_kill_switch_active
        brain = get_brain_state()
        kill = is_kill_switch_active()
        mode = BOT_MODE
        strategy = BOT_STRATEGY
        watchlist = WATCHLIST
        cycle_interval = CYCLE_INTERVAL
        next_cycle_at = brain.get("next_cycle_at")
    except Exception as exc:
        brain = {"current_action": "unknown", "last_reasoning": [str(exc)], "next_cycle_at": None}
        kill = False
        mode = os.getenv("BOT_MODE", "paper")
        strategy = os.getenv("BOT_STRATEGY", "premium_harvest_v1")
        watchlist = os.getenv("BOT_WATCHLIST", "SCHB,SPY,QQQ").split(",")
        cycle_interval = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))
        next_cycle_at = None

    # Min premium / max open from env
    min_premium = float(os.getenv("MIN_PREMIUM", "0.50"))
    max_open = int(os.getenv("MAX_OPEN_POSITIONS", "10"))

    # Seconds until next cycle
    secs_until_cycle = None
    if next_cycle_at:
        try:
            nca = datetime.fromisoformat(next_cycle_at)
            secs_until_cycle = max(0, int((nca - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            pass

    return jsonify({
        "mode": mode,
        "strategy": strategy,
        "current_action": brain["current_action"],
        "last_reasoning": brain["last_reasoning"],
        "kill_switch_active": kill,
        "market_open": mkt["market_open"],
        "seconds_until_next_event": mkt["seconds_until_next_event"],
        "next_event": mkt["next_event"],
        "server_utc": mkt["server_utc"],
        "next_cycle_at": next_cycle_at,
        "secs_until_cycle": secs_until_cycle,
        "cycle_interval_seconds": cycle_interval,
        "watchlist": watchlist,
        "min_premium": min_premium,
        "max_open": max_open,
    })


# ---------------------------------------------------------------------------
# Server-Sent Events stream
# ---------------------------------------------------------------------------

@dashboard.get("/stream")
def event_stream():
    """SSE stream - push every new event to connected browsers in real-time."""
    q = event_log.subscribe_sse()

    def generate():
        # Send a heartbeat immediately so browser knows we're connected
        yield "data: {\"type\": \"heartbeat\", \"ts\": \"" + datetime.now(timezone.utc).isoformat() + "\"}\n\n"
        try:
            while True:
                try:
                    import queue
                    payload = q.get(timeout=15)
                    yield payload
                except queue.Empty:
                    # Heartbeat to keep connection alive
                    yield "data: {\"type\": \"heartbeat\", \"ts\": \"" + datetime.now(timezone.utc).isoformat() + "\"}\n\n"
        except GeneratorExit:
            pass
        finally:
            event_log.unsubscribe_sse(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------

@dashboard.post("/kill")
def dashboard_kill():
    """Engage the kill switch from the dashboard."""
    try:
        from risk.guards import enable_kill_switch
        enable_kill_switch()
        event_log.record("kill_switch", "SYSTEM", action="enabled",
                        reasons=["Kill switch engaged via dashboard"])
        return jsonify({"ok": True, "message": "Kill switch enabled - no new orders will be placed"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@dashboard.post("/resume")
def dashboard_resume():
    """Disengage the kill switch from the dashboard."""
    try:
        from risk.guards import disable_kill_switch
        disable_kill_switch()
        event_log.record("kill_switch", "SYSTEM", action="disabled",
                        reasons=["Kill switch disabled via dashboard - bot will resume on next cycle"])
        return jsonify({"ok": True, "message": "Kill switch disabled - bot will resume on next cycle"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
