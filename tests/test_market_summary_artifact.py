"""Offline producer-to-publisher regression for reused market-review artifacts."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main
from scripts.publish_analysis_artifact import publish_reports, report_exit_code
from src.config import Config
from src.core.market_review import MarketReviewRunResult, run_market_review
from tests.test_us_daily_report_contract import (
    UNIVERSE, make_result, publisher_env, write_fixture,
)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        stamp = datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc)
        return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def report_environment(monkeypatch):
    monkeypatch.setenv("ANALYSIS_REFERENCE_TIME", "2026-10-29T00:30:00+00:00")
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-market-summary")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_SHA", "offline-commit")


def validation_env():
    return publisher_env(
        GITHUB_RUN_ID="offline-market-summary", GITHUB_RUN_ATTEMPT="1",
        GITHUB_REF_NAME="main", GITHUB_SHA="offline-commit",
    )


@pytest.mark.parametrize("route", ["during_stocks", "lock_fallback", "fresh_review"])
def test_full_analysis_produces_market_sidecar_before_artifact_validation(tmp_path, route):
    write_fixture(tmp_path)
    (tmp_path / "market_review_20261028.md").unlink()
    (tmp_path / "market_review_20261028.json").unlink()
    report = "## Synthetic US market review\n\nCompleted-session analysis."
    payload = {
        "region": "us", "language": "zh", "date": "2026-10-28",
        "indices": [{"code": "SPX", "current": 110, "prev_close": 100, "change_pct": 10}],
        "news": [{"title": "Synthetic news", "url": "https://example.org/news"}],
        "sections": [{"key": "overview", "title": "Overview", "markdown": report}],
        "markdown_report": report,
        "numeric_validation": {"equity_mean_pct": 10},
        "private_account": "PRIVATE_SENTINEL",
    }
    config = Config(
        stock_list=list(UNIVERSE), market_review_region="us",
        market_review_enabled=True, daily_market_context_enabled=True,
        trading_day_check_enabled=False, merge_email_notification=False,
        single_stock_notify=False, analysis_delay=0, backtest_enabled=False,
    )
    args = SimpleNamespace(
        workers=1, dry_run=False, no_notify=True, no_market_review=False,
        force_run=False, single_notify=False, no_context_snapshot=False,
        schedule=False, portfolio=None,
    )
    notifier = MagicMock()
    notifier.is_available.return_value = False

    def save_report(content, filename):
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    notifier.save_report_to_file.side_effect = save_report
    pipeline = MagicMock()
    pipeline.notifier = notifier
    pipeline.db.get_analysis_history.return_value = []
    pipeline._daily_market_context_service = None
    pipeline._last_local_report_path = str(tmp_path / "report_20261028.md")
    results = [make_result(code) for code in UNIVERSE]
    contexts = []

    def build_pipeline(**kwargs):
        pipeline.query_id = kwargs["query_id"]
        return pipeline

    def generate_context():
        context = pipeline._daily_market_context_service.get_context(
            region="us", config=config, notifier=notifier,
            allow_generate=True, persist_market_review_history=False,
            target_date=date(2026, 10, 28), current_query_id=pipeline.query_id,
        )
        assert context is not None
        contexts.append(context)

    def analyze_stocks(**kwargs):
        if route == "during_stocks":
            generate_context()
        return results

    pipeline.run.side_effect = analyze_stocks

    def review_after_stocks(*args, **kwargs):
        if route == "lock_fallback":
            generate_context()
            return None
        return run_market_review(
            notifier, config=config, send_notification=False, persist_history=False,
        )

    review_result = MarketReviewRunResult(report, payload)
    with (
        patch("main.datetime", FrozenDateTime),
        patch("main._resolve_portfolio_stock_codes", return_value=None),
        patch("main._refresh_stock_index_cache_for_analysis"),
        patch("main.record_analysis_run_marker"),
        patch("main._run_auto_backtest"),
        patch("src.core.pipeline.StockAnalysisPipeline", side_effect=build_pipeline),
        patch("main._run_market_review_with_shared_lock", side_effect=review_after_stocks) as fresh,
        patch("src.services.daily_market_context.try_acquire_market_review_lock", return_value=object()),
        patch("src.services.daily_market_context.release_market_review_lock"),
        patch("src.services.daily_market_context.run_market_review", return_value=review_result) as generated,
        patch("src.core.market_review.MarketAnalyzer") as analyzer,
        patch("src.core.market_review.datetime", FrozenDateTime),
        patch("src.feishu_doc.FeishuDocManager") as docs,
    ):
        analyzer.return_value.run_daily_review_with_snapshot.return_value = SimpleNamespace(
            report=report, structured_payload=payload, market_light_snapshot={},
        )
        docs.return_value.is_configured.return_value = False
        assert main.run_full_analysis(
            config, args, stock_codes=list(UNIVERSE), raise_errors=True,
            refresh_watchlist=False,
        )

    assert generated.call_count == (0 if route == "fresh_review" else 1)
    assert fresh.call_count == (0 if route == "during_stocks" else 1)
    assert analyzer.call_count == (1 if route == "fresh_review" else 0)
    notifier.send.assert_not_called()
    for context in contexts:
        assert context.market_review_payload["indices"] == payload["indices"]
        assert "market_review_payload" not in context.to_safe_dict()

    # File date is the UTC generation date, not the completed US session date.
    sidecar = tmp_path / "market_review_20261029.json"
    assert sidecar.is_file()
    public = json.loads(sidecar.read_text(encoding="utf-8"))
    assert public["date"] == "2026-10-28"
    assert public["indices"] == payload["indices"]
    assert public["news"] == payload["news"]
    assert public["numeric_validation"] == payload["numeric_validation"]
    assert public["workflow_run_id"] == "offline-market-summary"
    assert "PRIVATE_SENTINEL" not in sidecar.read_text(encoding="utf-8")
    outcome = publish_reports(tmp_path, validation_env())
    assert outcome["status"] == "complete" and outcome["issues"] == []
    assert outcome["counts"] == {"requested": 15, "analyzed": 15, "failed": 0}
    assert report_exit_code(outcome) == 0
    for name in ("run.json", "summary.json", "report.md", "market_review.md", "market_review.json"):
        assert (tmp_path / "latest" / name).is_file()


@pytest.mark.parametrize("extension", ["md", "json"])
@pytest.mark.parametrize("content", [None, "", " \n\t"])
def test_missing_or_blank_market_artifacts_remain_failures(tmp_path, extension, content):
    write_fixture(tmp_path)
    path = tmp_path / f"market_review_20261028.{extension}"
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    outcome = publish_reports(tmp_path, validation_env())
    assert outcome["status"] == "failed"
    assert outcome["issues"]
    assert report_exit_code(outcome) == 1


def test_reused_summary_export_failure_does_not_bypass_validation(tmp_path):
    write_fixture(tmp_path)
    (tmp_path / "market_review_20261028.json").unlink()
    notifier = MagicMock()
    notifier.save_report_to_file.return_value = str(tmp_path / "market_review_20261028.md")
    with patch("src.services.report_run_context.write_market_summary", side_effect=OSError("synthetic disk error")):
        main._save_reused_market_review_report(
            notifier, "# Synthetic complete market report", config=SimpleNamespace(report_language="zh"),
            trigger_source="cli", region="us", market_review_payload={"region": "us", "date": "2026-10-28"},
        )
    outcome = publish_reports(tmp_path, validation_env())
    assert outcome["status"] == "failed"
    assert "market_summary_missing" in outcome["issues"]
    assert report_exit_code(outcome) == 1
