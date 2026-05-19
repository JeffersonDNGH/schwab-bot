"""bot.py
Main trading loop — wires together market data, strategy, risk, and orders.
Runs on a configurable cycle interval (default 5 min).
"""

import os
import time
import logging
from datetime import datetime, timezone

from strategies.premium_harvest import evaluate
from risk.guards import can_trade, is_kill_switch_active
from schwab_account import SchwabAccount
from schwab_market_data import SchwabMarketData
from schwab_orders import SchwabOrders
from app import event_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment (injected by systemd via SCHWAB_BOT_ENV secret)
# ---------------------------------------------------------------------------
WATCHLIST = [s.strip() for s in os.getenv("BOT_WATCHLIST", "SPY,QQQ,IWM,GLD,TLT").split(",") if s.strip()]
BOT_MODE = os.getenv("BOT_MODE", "paper")          # 'paper' | 'live'
BOT_STRATEGY = os.getenv("BOT_STRATEGY", "premium_harvest_v1")
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))  # 5 min default
TARGET_DELTA = float(os.getenv("TARGET_DELTA", "0.30"))
TARGET_DTE = int(os.getenv("TARGET_DTE", "30"))
PROFIT_TAKE_PCT = float(os.getenv("PROFIT_TAKE_PCT", "0.50"))  # close at 50% profit

# Shared state
_last_cycle: dict = {}
_cycle_count: int = 0

# ---------------------------------------------------------------------------
# BOT BRAIN STATE — written each cycle, read by /api/brain (no logic changes)
# ---------------------------------------------------------------------------
_bot_state: dict = {
    "current_action": "initializing",   # human-readable one-liner
    "last_reasoning": [],               # last 1-2 medium-verbosity lines
    "next_cycle_at": None,              # ISO timestamp (UTC) of next scheduled run
    "state_updated_at": None,           # ISO timestamp of last write
}

def _set_state(action: str, reasoning: list[str] | None = None) -> None:
    """Write to _bot_state. Called throughout _run_cycle — pure side effect."""
    _bot_state["current_action"] = action
    if reasoning is not None:
        _bot_state["last_reasoning"] = reasoning[-2:]  # keep last 2 lines max
    _bot_state["state_updated_at"] = datetime.now(timezone.utc).isoformat()


def get_brain_state() -> dict:
    """Return a snapshot of bot brain state for the /api/brain route."""
    return {
        "current_action": _bot_state["current_action"],
        "last_reasoning": _bot_state["last_reasoning"],
        "next_cycle_at": _bot_state["next_cycle_at"],
        "state_updated_at": _bot_state["state_updated_at"],
    }

# ---------------------------------------------------------------------------
# Module-level clients (created once per process)
# ---------------------------------------------------------------------------
_account = SchwabAccount()
_market = SchwabMarketData()
_orders = SchwabOrders(account=_account)


def get_status() -> dict:
    return {
        "mode": BOT_MODE,
        "strategy": BOT_STRATEGY,
        "watchlist": WATCHLIST,
        "kill_switch": is_kill_switch_active(),
        "cycle_count": _cycle_count,
        "last_cycle": _last_cycle,
    }


def _market_is_open() -> bool:
    """Rough check: NYSE is open Mon–Fri 09:30–16:00 ET.
    A production version should call Schwab's market hours endpoint.
    """
    now = datetime.now(timezone.utc)
    # UTC offset for ET: -5 (EST) or -4 (EDT) — use -5 conservatively
    et_hour = (now.hour - 5) % 24
    et_minute = now.minute
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    open_minutes = et_hour * 60 + et_minute
    return 9 * 60 + 30 <= open_minutes <= 16 * 60


def _run_cycle() -> None:
    """Execute one full strategy cycle."""
    global _cycle_count, _last_cycle

    _cycle_count += 1
    log.info("=== Cycle %d start ===", _cycle_count)

    if is_kill_switch_active():
        log.warning("Kill switch active — skipping cycle.")
        _set_state("kill_switch_active", ["Kill switch is engaged — all trading halted"])
        return

    if not _market_is_open():
        log.info("Market closed — skipping cycle.")
        _set_state("market_closed_idling", ["Market closed — skipping cycle"])
        return

    # ---- Account snapshot ----
    _set_state("scanning_watchlist", ["Fetching account snapshot"])
    try:
        buying_power = _account.get_buying_power()
        equity_positions = _account.get_equity_positions()
        option_positions = _account.get_option_positions()
    except Exception as exc:
        log.error("Account fetch failed: %s", exc)
        _set_state("error", [f"Account fetch failed: {exc}"])
        return

    log.info("Buying power: $%.2f | Equity positions: %d | Open options: %d",
             buying_power, len(equity_positions), len(option_positions))

    # ---- Risk gate ----
    if not can_trade(buying_power=buying_power, open_option_count=len(option_positions)):
        log.warning("Risk guard blocked trading this cycle.")
        _set_state("risk_blocked", ["Risk guard blocked trading this cycle"])
        return

    # ---- Strategy evaluation ----
    actions = []
    reasoning_lines = []
    for symbol in WATCHLIST:
        try:
            price = _market.get_price(symbol)
            if price <= 0:
                log.warning("Bad price for %s: %.2f", symbol, price)
                reasoning_lines.append(f"Skipped {symbol}: bad price {price:.2f}")
                continue

            # Covered call: only if we hold >=100 shares of this ETF
            shares_held = sum(
                int(p.get("longQuantity", 0))
                for p in equity_positions
                if p.get("instrument", {}).get("symbol") == symbol
            )

            if shares_held >= 100:
                call_contract = _market.get_best_otm_strike(
                    symbol, option_type="CALL",
                    target_delta=TARGET_DELTA, dte=TARGET_DTE
                )
                if call_contract:
                    signal = evaluate(
                        symbol=symbol,
                        price=price,
                        option_type="CALL",
                        contract=call_contract,
                        buying_power=buying_power,
                        existing_option_positions=option_positions,
                    )
                    if signal.get("action") == "sell_to_open":
                        result = _orders.sell_to_open_call(
                            option_symbol=call_contract["symbol"],
                            quantity=signal["quantity"],
                            limit_price=call_contract["bid"],
                        )
                        actions.append({"type": "covered_call", "symbol": symbol, "result": result})
                        reasoning_lines.append(
                            f"Placed CALL {symbol} @ ${call_contract['bid']:.2f}"
                        )
                    else:
                        best_bid = call_contract.get("bid", 0)
                        reasoning_lines.append(
                            f"Scanned {symbol} CALL: best premium ${best_bid:.2f} — {signal.get('reason', 'no signal')}"
                        )
                else:
                    reasoning_lines.append(f"Scanned {symbol} CALL: no contract found")

            # Cash-secured put: if cash available
            if buying_power >= 5_000:
                put_contract = _market.get_best_otm_strike(
                    symbol, option_type="PUT",
                    target_delta=TARGET_DELTA, dte=TARGET_DTE
                )
                if put_contract:
                    signal = evaluate(...)
                    if signal.get("action") == "sell_to_open":
                        result = _orders.sell_to_open_put(...)
                        actions.append({"type": "csp", "symbol": symbol, "result": result})
                        reasoning_lines.append(
                            f"Placed PUT {symbol} @ ${put_contract.get('bid', 0):.2f}"
                        )
                    else:
                        best_bid = put_contract.get("bid", 0)
                        reasoning_lines.append(
                            f"Scanned {symbol} PUT: best premium ${best_bid:.2f} — {signal.get('reason', 'no signal')}"
                        )
                else:
                    reasoning_lines.append(f"Scanned {symbol} PUT: no contract found")

        except Exception as exc:
            log.error("Error processing %s: %s", symbol, exc)
            reasoning_lines.append(f"Error on {symbol}: {exc}")
            continue

    # Write final cycle state
    if actions:
        _set_state("scanning_watchlist", reasoning_lines)
    else:
        _set_state("scanning_watchlist", reasoning_lines if reasoning_lines else ["Scanned watchlist: no signals this cycle"])

    _last_cycle = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle": _cycle_count,
        "buying_power": buying_power,
        "actions_taken": len(actions),
        "actions": actions,
    }
    log.info("=== Cycle %d done. Actions taken: %d ===", _cycle_count, len(actions))


def run() -> None:
    """Blocking main loop called by app/main.py."""
    log.info("Bot starting. mode=%s strategy=%s watchlist=%s", BOT_MODE, BOT_STRATEGY, WATCHLIST)
    _set_state("starting_up", ["Bot process starting"])
    while True:
        try:
            # Mark next cycle time BEFORE sleeping so the dashboard can count down
            next_cycle_ts = datetime.now(timezone.utc).timestamp() + CYCLE_INTERVAL
            _bot_state["next_cycle_at"] = datetime.fromtimestamp(
                next_cycle_ts, tz=timezone.utc
            ).isoformat()

            _run_cycle()

            # After cycle, mark as waiting
            if not is_kill_switch_active():
                if _market_is_open():
                    _set_state("waiting_next_cycle", _bot_state["last_reasoning"])
                else:
                    _set_state("market_closed_idling", ["Market closed — idling until open"])

        except KeyboardInterrupt:
            log.info("Shutdown requested.")
            break
        except Exception as exc:
            log.exception("Unhandled error in cycle: %s", exc)
            _set_state("error", [f"Unhandled error: {exc}"])
        time.sleep(CYCLE_INTERVAL)
