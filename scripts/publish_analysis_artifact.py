# -*- coding: utf-8 -*-
"""Publish run-scoped report aliases and validate explicit partial outcomes.

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

from src.services.report_run_context import identity_issues, report_run_context
from src.services.report_validation import validate_price_change


def expected_outputs(env):
    # Reuse the actual CLI target parsing and trading-day contract.
    from main import _classify_stock_list_tokens, _compute_trading_day_filter
    from src.services.stock_list_parser import split_stock_list

    if env.get("SCHEDULE_SHOULD_RUN") == "false":
        return [], False, True
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
    for alias in ("report.md", "market_review.md", "market_review.json", "summary.json", "run.json"):
        (latest / alias).unlink(missing_ok=True)
    run = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "workflow": ".github/workflows/00-daily-analysis.yml", "status": "failed", "issues": [], "files": {}}
    run.update(report_run_context(env))
    run.update({"requested_codes": [], "analyzed_codes": [], "failed_codes": [],
                "counts": {"requested": 0, "analyzed": 0, "failed": 0}})
    issues = run["issues"]
    try:
        codes, review_expected, skipped = expected_outputs(env)
        from src.services.stock_list_parser import split_stock_list
        run["expected_codes"] = list(dict.fromkeys(code.upper() for code in split_stock_list(
            env.get("STOCK_LIST_CONFIG") or env.get("STOCK_LIST") or "600519")))
        step_ok = (env.get("ANALYSIS_STEP_OUTCOME", "success") == "success"
                   or (skipped and env.get("SCHEDULE_SHOULD_RUN") == "false"))
        if not step_ok:
            issues.append("analysis_step_failed")
        if skipped and step_ok:
            run["status"] = "skipped"
            run["skip_reason"] = env.get("SCHEDULE_SKIP_REASON") or "non_trading_day"
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
                    issues.extend(identity_issues(summary, run))
                    for key, source_key in (("workflow_run_id", "GITHUB_RUN_ID"), ("workflow_run_attempt", "GITHUB_RUN_ATTEMPT")):
                        if env.get(source_key) and str(summary.get(key)) != str(env[source_key]):
                            issues.append("stock_summary_run_mismatch")
                    requested = summary.get("requested_codes") or []
                    analyzed = summary.get("analyzed_codes") or []
                    records = summary.get("stocks") or []
                    run.update({key: summary.get(key) for key in ("requested_codes", "analyzed_codes", "failed_codes")})
                    run["data_quality"] = summary.get("data_quality")
                    if set(requested) != set(codes):
                        issues.append("stock_universe_mismatch")
                    if summary.get("expected_codes") != requested:
                        issues.append("stock_expected_codes_mismatch")
                    failed = summary.get("failed_codes") or []
                    expected_status = "complete" if not failed else "partial" if analyzed else "failed"
                    requested_set, analyzed_set, failed_set = set(requested), set(analyzed), set(failed)
                    record_codes = [item.get("code") for item in records]
                    if (summary.get("status") != expected_status
                            or len(requested_set) != len(requested) or len(analyzed_set) != len(analyzed)
                            or len(failed_set) != len(failed) or analyzed_set & failed_set
                            or analyzed_set | failed_set != requested_set
                            or summary.get("counts") != {"requested": len(requested), "analyzed": len(analyzed), "failed": len(failed)}
                            or len(records) != len(requested) or set(record_codes) != requested_set
                            or any(item.get("status") != ("analyzed" if item.get("code") in analyzed_set else "failed") for item in records)
                            or any(item.get("score") is not None for item in records if item.get("code") in failed_set)
                            or any(item.get("failure_reason") not in {"rate_limited", "provider_unavailable", "timeout", "analysis_failed", "no_result"}
                                   for item in records if item.get("code") in failed_set)):
                        issues.append("stock_summary_inconsistent")
                    elif failed:
                        issues.append("stock_coverage_incomplete")
                    for item in records:
                        if item.get("status") == "analyzed":
                            if not isinstance(item.get("quote"), dict) or not isinstance(item.get("data_quality"), dict):
                                issues.append("stock_quality_missing")
                                break
                            if validate_price_change(item["quote"])["status"] == "mismatch":
                                issues.append("stock_price_arithmetic_mismatch")
                                break
            if review_expected and market_report is None:
                issues.append("market_review_missing")
            elif review_expected:
                market_json = market_report.with_suffix(".json")
                if not market_json.is_file():
                    issues.append("market_summary_missing")
                else:
                    market_summary = json.loads(market_json.read_text(encoding="utf-8"))
                    issues.extend(identity_issues(market_summary, run))
                    if market_summary.get("schema_version") != 1:
                        issues.append("market_summary_schema_mismatch")
                    if env.get("MARKET_REVIEW_REGION", "us") == "us" and market_summary.get("date") != run["trading_session_date"]:
                        issues.append("market_bar_date_mismatch")
                    shutil.copyfile(market_json, latest / "market_review.json")
                    run["files"]["market_review.json"] = market_json.name
            for alias in tuple(run["files"]):
                if not (latest / alias).is_file() or (latest / alias).stat().st_size == 0:
                    issues.append("required_file_missing:" + alias)
            no_success = bool(codes) and (run.get("counts") or {}).get("analyzed", 0) == 0
            structural_errors = set(issues) - {"stock_coverage_incomplete"}
            run["status"] = "failed" if no_success or structural_errors else "partial" if issues else "complete"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        issues.append("report_validation_error")
    (latest / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    line = f"Report status: {run['status']}; issues: {', '.join(issues) or 'none'}.\n"
    print(line.strip())
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("## Analysis artifact validation\n\n" + line)
    return run


def report_exit_code(outcome):
    """Provider failures are explicit partial results; structural failures remain errors."""
    if outcome["status"] in {"complete", "skipped"}:
        return 0
    counts = outcome.get("counts") or {}
    if (outcome["status"] == "partial" and counts.get("analyzed", 0) > 0
            and counts.get("failed", 0) > 0
            and counts.get("requested") == counts.get("analyzed", 0) + counts.get("failed", 0)
            and set(outcome.get("issues") or []) == {"stock_coverage_incomplete"}):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(report_exit_code(publish_reports()))
