"""Public run identity and exchange-session metadata. No credentials or accounts."""
from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path

WORKFLOW_PATH = ".github/workflows/00-daily-analysis.yml"


def aware_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def report_run_context(env=None, now=None):
    from src.core.trading_calendar import build_market_phase_context, resolve_historical_daily_bar_date

    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    # Freeze once per workflow, not once per stock; crossing UTC midnight must
    # not change a run's expected trading session or identity.
    reference = aware_timestamp(env.get("ANALYSIS_REFERENCE_TIME")) or now
    phase = build_market_phase_context(market="us", current_time=reference)
    session = resolve_historical_daily_bar_date("us", phase.session_date, phase.phase.value)
    return {
        "workflow": WORKFLOW_PATH,
        "workflow_run_id": env.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "source_branch": env.get("GITHUB_REF_NAME"),
        "commit_sha": env.get("GITHUB_SHA"),
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF"),
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "reference_time": reference.isoformat(),
        "run_started_at": env.get("ANALYSIS_REFERENCE_TIME"),
        "trading_session_date": session.isoformat() if session else None,
        "session_market": "us",
    }


def identity_issues(document, expected):
    issues = []
    workflow_ref = expected.get("workflow_ref")
    if workflow_ref and f"/{WORKFLOW_PATH}@" not in workflow_ref:
        issues.append("actual_workflow_path_mismatch")
    if workflow_ref and expected.get("source_branch") and not workflow_ref.endswith("@refs/heads/" + expected["source_branch"]):
        issues.append("workflow_branch_mismatch")
    for key in ("workflow", "workflow_run_id", "workflow_run_attempt", "source_branch", "commit_sha", "workflow_ref"):
        if expected.get(key) and str(document.get(key)) != str(expected[key]):
            issues.append("identity_mismatch:" + key)
    generated = aware_timestamp(document.get("generated_at"))
    reference = aware_timestamp(expected.get("run_started_at"))
    now = aware_timestamp(expected.get("generated_at"))
    if generated is None or (reference and generated < reference) or (now and (generated - now).total_seconds() > 60):
        issues.append("generation_timestamp_invalid")
    if (expected.get("trading_session_date") is None
            or document.get("trading_session_date") != expected["trading_session_date"]):
        issues.append("trading_session_mismatch")
    return issues


def write_market_summary(report_path, payload):
    """Export only public market data; omit internal diagnostics and model bodies."""
    allowed = ("region", "language", "date", "indices", "news", "market_phase_context", "numeric_validation")
    summary = {key: payload.get(key) for key in allowed if key in payload}
    summary.update(report_run_context())
    summary["schema_version"] = 1
    Path(report_path).with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
