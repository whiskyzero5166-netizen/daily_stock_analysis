"""Offline calendar preflight for normal scheduled US analysis; never dispatches."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def schedule_decision(event_name, current_time):
    from src.core.trading_calendar import MarketPhase, build_market_phase_context

    if event_name != "schedule":
        return True, "manual"
    context = build_market_phase_context(market="us", current_time=current_time)
    if context.phase == MarketPhase.UNKNOWN:
        raise ValueError("us_calendar_unavailable")
    if context.phase == MarketPhase.NON_TRADING:
        return False, "us_market_holiday_or_weekend"
    if context.phase != MarketPhase.POSTMARKET:
        return False, "before_regular_close"
    return True, "us_post_close"


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    proceed, reason = schedule_decision(os.getenv("GITHUB_EVENT_NAME"), now)
    values = {"should_run": str(proceed).lower(), "skip_reason": reason,
              "reference_time": now.isoformat()}
    print("US calendar preflight: " + reason)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in values.items())
