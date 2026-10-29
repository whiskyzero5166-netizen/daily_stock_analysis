# -*- coding: utf-8 -*-
"""Publish run-scoped stable report aliases and reject incomplete green runs.

This script reads outputs only. It never starts analysis, sends notifications,
loads portfolio snapshots, or copies the process environment into artifacts.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def expected_outputs(env):
    # Reuse the actual CLI target parsing and trading-day contract.
    from main import _classify_stock_list_tokens, _compute_trading_day_filter
    from src.services.stock_list_parser import split_stock_list

    mode = env.get("ANALYSIS_MODE", "full")
    tokens = split_stock_list(env.get("STOCK_LIST_CONFIG") or env.get("STOCK_LIST") or "600519")
    classified = _classify_stock_list_tokens(tokens, entry_label="Artifact validation")
    if classified is None:
        raise ValueError("unsupported_stock_list")
    codes = [] if mode == "market-only" else classified[0]
    config = SimpleNamespace(
        trading_day_check_enabled=env.get("TRADING_DAY_CHECK_ENABLED", "true").lower() == "true",
        market_review_enabled=env.get("MARKET_REVIEW_ENABLED", "true").lower() == "true",
        market_review_region=env.get("MARKET_REVIEW_REGION", "us"),
    )
    args = SimpleNamespace(force_run=env.get("FORCE_RUN", "false").lower() == "true", no_market_review=mode == "stocks-only")
    codes, region, skip = _compute_trading_day_filter(config, args, codes)
    review = config.market_review_enabled and not args.no_market_review and region != ""
    return list(dict.fromkeys(codes)), review, skip


def publish_reports(reports_dir=None, env=None):
    env = os.environ if env is None else env
    reports = Path(reports_dir) if reports_dir is not None else ROOT / "reports"
    latest = reports / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for alias in ("report.md", "market_review.md", "summary.json", "run.json"):
        (latest / alias).unlink(missing_ok=True)
    run = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "workflow": ".github/workflows/00-daily-analysis.yml", "status": "failed", "issues": [], "files": {}}
    run["workflow_run_id"] = env.get("GITHUB_RUN_ID")
    run["workflow_run_attempt"] = env.get("GITHUB_RUN_ATTEMPT")
    issues = run["issues"]
    try:
        codes, review_expected, skipped = expected_outputs(env)
        run["expected_codes"] = codes
        step_ok = env.get("ANALYSIS_STEP_OUTCOME", "success") == "success"
        if not step_ok:
            issues.append("analysis_step_failed")
        if skipped and step_ok:
            run["status"] = "skipped"
            run["skip_reason"] = "non_trading_day"
        else:
            def copy_latest(pattern, alias):
                paths = [path for path in reports.glob(pattern) if path.is_file() and path.stat().st_size > 0]
                if not paths:
                    return None
                source = max(paths, key=lambda path: path.stat().st_mtime_ns)
                shutil.copyfile(source, latest / alias)
                run["files"][alias] = source.name
                return source

            stock_report = copy_latest("report_????????.md", "report.md") if codes else None
            market_report = copy_latest("market_review_????????.md", "market_review.md") if review_expected else None
            if codes:
                if stock_report is None:
                    issues.append("stock_report_missing")
                elif not stock_report.with_suffix(".json").is_file():
                    issues.append("stock_summary_missing")
                else:
                    summary_path = stock_report.with_suffix(".json")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    shutil.copyfile(summary_path, latest / "summary.json")
                    run["files"]["summary.json"] = summary_path.name
                    run["counts"] = summary.get("counts")
                    if summary.get("schema_version") != 1:
                        issues.append("stock_summary_schema_mismatch")
                    for key, source_key in (("workflow_run_id", "GITHUB_RUN_ID"), ("workflow_run_attempt", "GITHUB_RUN_ATTEMPT")):
                        if env.get(source_key) and str(summary.get(key)) != str(env[source_key]):
                            issues.append("stock_summary_run_mismatch")
                    requested = summary.get("requested_codes") or []
                    analyzed = summary.get("analyzed_codes") or []
                    records = summary.get("stocks") or []
                    if set(requested) != set(codes):
                        issues.append("stock_universe_mismatch")
                    if (summary.get("status") != "complete" or summary.get("failed_codes")
                            or set(analyzed) != set(codes) or len(analyzed) != len(codes)
                            or summary.get("counts") != {"requested": len(codes), "analyzed": len(codes), "failed": 0}
                            or len(records) != len(codes) or {item.get("code") for item in records} != set(codes)
                            or any(item.get("status") != "analyzed" for item in records)):
                        issues.append("stock_coverage_incomplete")
            if review_expected and market_report is None:
                issues.append("market_review_missing")
            run["status"] = "complete" if not issues else "partial" if run["files"] else "failed"
    except (OSError, ValueError, TypeError, KeyError):
        issues.append("report_validation_error")
    (latest / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    line = f"Report status: {run['status']}; issues: {', '.join(issues) or 'none'}.\n"
    print(line.strip())
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("## Analysis artifact validation\n\n" + line)
    return run


if __name__ == "__main__":
    outcome = publish_reports()
    raise SystemExit(0 if outcome["status"] in {"complete", "skipped"} else 1)
