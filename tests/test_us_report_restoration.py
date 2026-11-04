"""Offline regression cases for restoration, freshness, provenance and facts."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import os
from pathlib import Path

import pytest

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import UnifiedRealtimeQuote
from scripts.check_us_schedule import schedule_decision
from scripts.publish_analysis_artifact import publish_reports, report_exit_code
from src.core.trading_calendar import classify_us_quote_freshness
from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview
from src.search_service import SearchResponse, SearchResult, SearchService
from src.services.news_provenance import (
    canonical_news_url, deduplicate_news, financial_evidence, label_financial_claims,
    primary_source, public_financial_evidence,
)
from src.services.report_run_context import identity_issues, report_run_context
from src.services.report_validation import validate_price_change
from tests.test_us_daily_report_contract import legacy_report, make_result, publisher_env, write_fixture


def dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture(autouse=True)
def frozen_run(monkeypatch):
    monkeypatch.setenv("ANALYSIS_REFERENCE_TIME", "2026-10-28T22:30:00Z")


@pytest.mark.parametrize("quote,fetched,session,stale,reason", [
    ("2026-11-02T21:00:02Z", "2026-11-03T04:00:00Z", "close", False, "latest_completed_close"),
    ("2026-11-02T20:59:58Z", "2026-11-03T04:00:00Z", "close", False, "latest_completed_close"),
    ("2026-10-30T20:00:01Z", "2026-11-02T12:00:00Z", "close", False, "latest_completed_close"),
    ("2026-10-30T20:00:01Z", "2026-11-02T15:00:00Z", "close", True, "outdated_or_unconfirmed_close"),
    ("2026-10-30T20:00:01Z", "2026-11-03T04:00:00Z", "close", True, "outdated_or_unconfirmed_close"),
    ("2026-11-25T21:00:00Z", "2026-11-26T23:30:00Z", "close", False, "latest_completed_close"),
    ("2026-11-27T18:00:02Z", "2026-11-27T23:30:00Z", "close", False, "latest_completed_close"),
    ("2026-03-06T21:00:00Z", "2026-03-09T12:00:00Z", "close", False, "latest_completed_close"),
    ("2026-11-02T16:00:00Z", "2026-11-02T16:05:00Z", "intraday", False, "intraday"),
    ("2026-11-02T16:00:00Z", "2026-11-02T17:00:00Z", "intraday", True, "outdated_quote"),
    ("2026-11-02T13:00:00Z", "2026-11-02T13:02:00Z", "premarket", False, "premarket"),
    ("2026-11-02T23:00:00Z", "2026-11-02T23:02:00Z", "postmarket", False, "postmarket"),
    ("2026-11-02T16:00:00Z", "2026-11-02T23:00:00Z", "close", True, "outdated_or_unconfirmed_close"),
    ("2026-11-04T21:00:00Z", "2026-11-02T23:00:00Z", "close", True, "future_timestamp"),
])
def test_session_freshness(quote, fetched, session, stale, reason):
    result = classify_us_quote_freshness(dt(quote), dt(fetched), session)
    assert result["is_stale"] is stale and result["freshness"] == reason


def test_unknown_calendar_and_missing_timestamps_are_not_certified():
    assert classify_us_quote_freshness(None, dt("2026-11-03T04:00:00Z"), "close")["is_stale"] is None
    with patch("src.core.trading_calendar._XCALS_AVAILABLE", False):
        assert classify_us_quote_freshness(dt("2026-11-02T21:00:00Z"), dt("2026-11-03T04:00:00Z"), "close")["freshness"] == "unknown"
        with pytest.raises(ValueError, match="calendar"):
            schedule_decision("schedule", dt("2026-11-03T04:00:00Z"))


def test_metadata_keeps_partial_fields_and_actual_quote_age():
    quote = UnifiedRealtimeQuote(code="MSFT", market="us", quote_session="close", price=100,
                                 provider_timestamp="2026-11-02T21:00:01Z", data_quality="partial", missing_fields=["amount"])
    manager = DataFetcherManager.__new__(DataFetcherManager)
    with patch.object(manager, "_utc_now_iso", return_value="2026-11-03T04:00:00Z"):
        manager._enrich_realtime_quote(quote)
    assert quote.is_stale is False and quote.stale_seconds > 600
    assert quote.data_quality == "partial" and quote.missing_fields == ["amount"]


@pytest.mark.parametrize("stamp,should_run", [
    ("2026-11-02T23:30:00Z", True), ("2026-11-26T23:30:00Z", False),
    ("2026-11-27T23:30:00Z", True), ("2026-11-01T23:30:00Z", False),
    ("2026-11-02T18:30:00Z", False),
])
def test_normal_schedule_skips_holidays_weekends_and_before_close(stamp, should_run):
    assert schedule_decision("schedule", dt(stamp))[0] is should_run
    assert schedule_decision("workflow_dispatch", dt(stamp))[0] is True


@pytest.mark.parametrize("pct,status", [(10, "passed"), (10.1, "passed"), (10.1001, "mismatch"), ("N/A", "unverified")])
def test_price_change_tolerance(pct, status):
    assert validate_price_change({"price": "110.00", "prev_close": "100.00", "pct_chg": pct})["status"] == status


def market_fixture():
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.region = "us"
    overview = MarketOverview(date="2026-11-02", indices=[
        MarketIndex(code="SPX", name="S&P 500", current=99.49, prev_close=100, change_pct=-0.51),
        MarketIndex(code="IXIC", name="Nasdaq", current=98.93, prev_close=100, change_pct=-1.07),
        MarketIndex(code="DJI", name="Dow Jones", current=100.41, prev_close=100, change_pct=0.41),
        MarketIndex(code="VIX", name="VIX", current=108.91, prev_close=100, change_pct=8.91),
    ])
    return analyzer, overview


def test_vix_never_enters_equity_index_average_and_bad_claim_is_corrected():
    analyzer, overview = market_fixture()
    metrics = analyzer._us_numeric_validation(overview)
    assert metrics["mean_change_pct"] == pytest.approx(-0.39)
    assert metrics["included_codes"] == ["SPX", "IXIC", "DJI"]
    assert metrics["excluded_codes"] == ["VIX"]
    text = analyzer._validate_us_narrative("主要指数平均涨跌幅 +1.93%\n标普500 (+2.00%)", overview)
    assert "-0.39%" in text and "原文 +1.93%" in text and "-0.51%" in text
    assert len(overview.numeric_claim_checks) == 2
    assert all(row["status"] == "passed" for row in metrics["price_arithmetic"].values())


def test_forecasts_and_different_averages_are_not_certified_by_daily_returns():
    analyzer, overview = market_fixture()
    text = analyzer._validate_us_narrative("主要指数平均预期收益 +5.00%\n行业平均收益 +1.93%", overview)
    assert "+5.00%" in text and "unverified forecast" in text
    assert "+1.93%" in text and "Metric/source unverified" in text


def news(title, url, stamp="2026-11-02", provider="Brave"):
    item = SearchResult(title=title, snippet=title, url=url, source="Publisher", published_date=stamp)
    SearchResponse(query="synthetic", results=[item], provider=provider).mark_retrieved()
    return item


def test_news_provenance_survives_filtering_ranking_and_url_deduplication():
    a = news("Microsoft earnings", "https://microsoft.com/investor/earnings?utm_source=test", provider="Tavily")
    b = news("Microsoft earnings", "https://microsoft.com/investor/earnings", provider="Brave")
    rows = deduplicate_news([a, b])
    assert len(rows) == 1 and rows[0].search_providers == ["Tavily", "Brave"]
    assert a.url.endswith("utm_source=test") and a.original_published_date == "2026-11-02"
    service = SearchService.__new__(SearchService)
    response = service._normalize_and_limit_response(SearchResponse("x", rows, "Tavily"), max_results=3)
    assert response.results[0].search_provider == "Tavily"
    assert response.results[0].original_published_date == "2026-11-02"
    assert a.url in a.to_text() and "verification: unverified" in a.to_text()
    assert not primary_source("https://microsoft.com.example.org/investor/")
    assert canonical_news_url("https://example.org/page?api_key=PRIVATE_SENTINEL") == ""


def test_comprehensive_fallback_and_cross_provider_dedup_do_not_expand_budget():
    service = SearchService(bocha_keys=["OFFLINE_TEST_KEY"], searxng_public_instances_enabled=False, news_max_age_days=30)
    stamp = datetime.now().date().isoformat()
    providers = [
        SimpleNamespace(name="Tavily", is_available=True, search=MagicMock(return_value=SearchResponse("x", [], "Tavily", success=False))),
        SimpleNamespace(name="Brave", is_available=True, search=MagicMock(return_value=SearchResponse("x", [news("Microsoft MSFT earnings guidance", "https://microsoft.com/investor/synthetic", stamp, "Brave")], "Brave"))),
        SimpleNamespace(name="SerpAPI", is_available=True, search=MagicMock(return_value=SearchResponse("x", [news("Microsoft MSFT earnings guidance", "https://microsoft.com/investor/synthetic?utm_source=fixture", stamp, "SerpAPI")], "SerpAPI"))),
    ]
    service._providers = providers
    with patch("src.search_service.time.sleep"):
        responses = service.search_comprehensive_intel("MSFT", "Microsoft", max_searches=3)
    assert sum(provider.search.call_count for provider in providers) == 3
    assert all(provider.search.call_count == 1 for provider in providers)
    assert sum(len(response.results) for response in responses.values()) == 1
    text = service.format_intel_report(responses, "Microsoft")
    assert "Brave, SerpAPI" in text and text.count("https://microsoft.com/investor/synthetic") == 1


def test_guidance_dates_are_scoped_to_same_issuer_and_never_certified_from_search():
    old = news("Microsoft earnings guidance", "https://microsoft.com/investor/old", "2026-07-20")
    new = news("Microsoft earnings guidance", "https://microsoft.com/investor/new", "2026-10-28")
    other = news("Amazon earnings guidance", "https://amazon.com/investor/new", "2026-11-02")
    unknown = news("Analyst consensus valuation", "https://example.org/story", None)
    records = financial_evidence([old, new, other, unknown], dt("2026-11-03T04:00:00Z"))
    assert records[0]["superseded_by"] == new.url
    assert "superseded_by" not in records[1]
    assert records[-1]["evidence_status"] == "missing_date"
    assert all(item["verification_status"] == "unverified" for item in records)
    result = make_result()
    result.fundamental_analysis = "最新指引显示每股收益10美元。latest guidance: $10."
    original_score = result.sentiment_score
    label_financial_claims(result, records)
    assert "最新指引" not in result.fundamental_analysis and "latest guidance" not in result.fundamental_analysis
    assert "unverified" in result.fundamental_analysis and result.sentiment_score == original_score
    public = public_financial_evidence([{**records[0], "holdings": "PRIVATE_SENTINEL", "api_key": "PRIVATE_SENTINEL"}])
    assert "PRIVATE_SENTINEL" not in json.dumps(public)


@pytest.mark.parametrize("key,value", [
    ("workflow_run_id", "old"), ("workflow_run_attempt", "0"),
    ("source_branch", "other"), ("workflow", "wrong.yml"), ("commit_sha", "other"),
    ("trading_session_date", "2026-10-27"), ("generated_at", "2020-01-01T00:00:00Z"),
])
def test_identity_mismatches_are_rejected(key, value):
    expected = report_run_context({"ANALYSIS_REFERENCE_TIME": "2026-10-28T22:30:00Z", "GITHUB_RUN_ID": "123",
                                  "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REF_NAME": "main", "GITHUB_SHA": "abc"})
    assert identity_issues({**expected, key: value}, expected)


@pytest.mark.parametrize("missing", ["report_20261028.md", "report_20261028.json", "market_review_20261028.md", "market_review_20261028.json"])
def test_missing_mode_required_artifacts_fail_even_with_15_successes(tmp_path, missing):
    write_fixture(tmp_path)
    (tmp_path / missing).unlink()
    result = publish_reports(tmp_path, publisher_env())
    assert result["status"] == "failed" and report_exit_code(result) == 1


def test_scheduled_holiday_has_explicit_skipped_manifest_and_no_analysis_outputs(tmp_path):
    result = publish_reports(tmp_path, publisher_env(SCHEDULE_SHOULD_RUN="false", ANALYSIS_STEP_OUTCOME="skipped", SCHEDULE_SKIP_REASON="us_market_holiday_or_weekend"))
    assert result["status"] == "skipped" and len(result["expected_codes"]) == 15
    assert result["counts"] == {"requested": 0, "analyzed": 0, "failed": 0}
    assert result["files"] == {} and (tmp_path / "latest/run.json").is_file()


def test_restored_report_visual_evidence(tmp_path):
    from src.core.market_profile import get_profile
    from src.services.report_renderer import render

    result = make_result()
    result.market_snapshot.update({"freshness": "latest_completed_close", "quote_session_date": "2026-10-28", "is_stale": False})
    sources = financial_evidence([news("Microsoft earnings guidance — synthetic example", "https://microsoft.com/investor/synthetic", "2026-10-28")])
    label_financial_claims(result, sources)
    stock = legacy_report([result])
    rendered = render("markdown", [result])
    for text in (stock, rendered):
        assert "https://microsoft.com/investor/synthetic" in text and "Brave" in text
        assert "latest_completed_close" in text and "未独立核实" in text and "partial" in text
    analyzer, overview = market_fixture()
    analyzer.profile = get_profile("us")
    analyzer.config = SimpleNamespace(report_language="zh", market_review_color_scheme="green_up")
    overview.analysis_time = "2026-11-03T04:00:00+00:00"
    overview.date_basis = "data_date"
    overview.market_phase_context = {"phase": "postmarket", "market_local_time": "2026-11-02T23:00:00-05:00", "effective_daily_bar_date": "2026-11-02"}
    market = analyzer._generate_template_review(overview, [])
    assert "-0.39%" in market and "VIX" in market
    folder = Path(os.getenv("REPORT_EVIDENCE_DIR", str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    warning = "> 合成测试样例；所有价格、信号和链接均为离线示例，不是实际行情。\n\n"
    (folder / "restored-stock-report.md").write_text(warning + stock, encoding="utf-8")
    (folder / "restored-market-report.md").write_text(warning + market, encoding="utf-8")
