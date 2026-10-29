# -*- coding: utf-8 -*-
"""Offline contracts for post-close US reports; all prices and account sentinels are synthetic."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.yfinance_fetcher import YfinanceFetcher, _with_yahoo_quote_metadata
from scripts.publish_analysis_artifact import publish_reports
from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.core.pipeline import StockAnalysisPipeline
from src.notification import NotificationService
from src.services.report_renderer import (
    market_snapshot_notes, normalize_price_result, render, write_analysis_summary,
)

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = "MRVL,MSFT,QQQ,NOW,TMO,TSM,ETN,ANET,AMZN,RUM,GOOGL,DRAM,LITX,VRT,ORCL,ORCX,AXSM,MVLL,REGN,HUBB,CEG,TT,ABBV,PWR,GEV,ILMN,TEM,CRSP,BEAM,QURE,BMRN,NTRA,GH,TWST,VRTX,ALNY".split(",")
REGULAR_TIME = int(datetime(2026, 10, 28, 20, tzinfo=timezone.utc).timestamp())


def workflow():
    return yaml.load((ROOT / ".github/workflows/00-daily-analysis.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def make_result(code="MSFT"):
    result = AnalysisResult(
        code=code, name="Synthetic fixture", sentiment_score=50, trend_prediction="震荡",
        operation_advice="观望", analysis_summary="合成测试，等待确认", technical_analysis="均线震荡",
        risk_warning="合成市场风险", data_sources="quote:yfinance,technical:partial",
        dashboard={
            "core_conclusion": {"one_sentence": "合成测试，等待确认", "position_advice": {"has_position": "观察90元支撑"}},
            "intelligence": {"risk_alerts": ["合成风险"], "positive_catalysts": ["合成催化因素"]},
            "battle_plan": {"sniper_points": {"stop_loss": "90元", "take_profit": "120元"}},
            "phase_decision": {"watch_conditions": ["放量突破110元"], "data_limitations": ["仅为合成样例"]},
        },
    )
    result.market_snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot({
        "code": code, "date": "2026-10-28",
        "today": {"close": 110, "pre_close": 100, "open": 101, "high": 112, "low": 99, "data_source": "realtime:yfinance", "is_estimated": True},
        "yesterday": {"close": 80},
        "realtime": {"price": 110, "pre_close": 100, "currency": "USD", "source": "yfinance", "quote_session": "close", "provider_timestamp": "2026-10-28T20:00:00+00:00", "fetched_at": "2026-10-28T22:30:00+00:00", "extended_price": 115, "extended_timestamp": "2026-10-28T22:00:00+00:00", "extended_session": "postmarket", "data_quality": "partial", "missing_fields": ["amount"]},
    })
    result.market_phase_summary = {"market": "us", "phase": "postmarket", "market_local_time": "2026-10-28T18:30:00-04:00", "session_date": "2026-10-28", "is_partial_bar": False}
    return result


def legacy_report(results):
    notifier = NotificationService.__new__(NotificationService)
    notifier._config = SimpleNamespace(report_show_llm_model=False)
    notifier._report_show_llm_model = False
    notifier._report_summary_only = False
    with patch("src.notification.get_config", return_value=SimpleNamespace(report_renderer_enabled=False)):
        return notifier.generate_dashboard_report(results)


def write_fixture(reports, count=36):
    reports.mkdir(parents=True, exist_ok=True)
    results = [make_result(code) for code in UNIVERSE[:count]]
    report = reports / "report_20261028.md"
    report.write_text(legacy_report(results) if results else "# 无成功分析\n", encoding="utf-8")
    failures = {code: "rate_limited" for code in UNIVERSE[count:]}
    summary = write_analysis_summary(report, results, UNIVERSE, failures)
    (reports / "market_review_20261028.md").write_text("# US market review — synthetic fixture\n", encoding="utf-8")
    return summary


def publisher_env(**updates):
    return {"STOCK_LIST_CONFIG": ",".join(UNIVERSE), "MARKET_REVIEW_REGION": "us", "TRADING_DAY_CHECK_ENABLED": "false", "ANALYSIS_MODE": "full", "ANALYSIS_STEP_OUTCOME": "success", **updates}


def test_schedule_dispatch_priorities_and_artifact_contract():
    wf = workflow()
    assert wf["on"]["schedule"] == [{"cron": "30 18 * * 1-5", "timezone": "America/New_York"}]
    inputs = wf["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["full", "market-only", "stocks-only"]
    assert inputs["force_run"]["type"] == "boolean" and inputs["force_run"]["default"] == "false"
    steps = wf["jobs"]["analyze"]["steps"]
    analysis = next(step for step in steps if step["name"] == "执行股票分析")
    for key, default in (("REPORT_TYPE", "full"), ("MARKET_REVIEW_REGION", "us"), ("REPORT_LANGUAGE", "zh")):
        assert analysis["env"][key] == "$" + "{{ vars." + key + " || secrets." + key + " || '" + default + "' }}"
    assert analysis["env"]["STOCK_LIST_CONFIG"] == "$" + "{{ vars.STOCK_LIST || secrets.STOCK_LIST }}"
    assert analysis["env"]["TRADING_DAY_CHECK_ENABLED"].endswith("|| 'true' }}")
    assert "--market-review" in analysis["run"] and "--no-market-review" in analysis["run"] and "--force-run" in analysis["run"]
    upload = next(step for step in steps if step["name"] == "上传分析报告")
    assert upload["with"]["name"] == "analysis-reports-" + "$" + "{{ github.run_number }}"
    assert upload["with"]["retention-days"] == "30" and upload["if"] == "always()"
    assert upload["with"]["path"].split() == ["reports/", "logs/"]
    gate = next(step for step in steps if step["name"] == "整理报告并验证覆盖率")
    assert gate["if"] == "always()" and steps.index(gate) < steps.index(upload)


@pytest.mark.parametrize("day,utc_hour,hk_hour", [("2026-03-06", 23, 7), ("2026-03-09", 22, 6), ("2026-10-30", 22, 6), ("2026-11-02", 23, 7)])
def test_dst_schedule_before_and_after_both_transitions(day, utc_hour, hk_hour):
    minute, hour, _, _, weekday = workflow()["on"]["schedule"][0]["cron"].split()
    local = datetime.fromisoformat(day).replace(hour=int(hour), minute=int(minute), tzinfo=ZoneInfo("America/New_York"))
    assert weekday == "1-5" and local.weekday() < 5
    assert local.astimezone(timezone.utc).hour == utc_hour
    hong_kong = local.astimezone(ZoneInfo("Asia/Hong_Kong"))
    assert (hong_kong.hour, hong_kong.minute) == (hk_hour, 30) and hong_kong.date() > local.date()


@pytest.mark.parametrize("configured,inherited,expected", [("MSFT,QQQ", "TMO", "MSFT,QQQ"), ("", "TMO", "TMO"), ("", "", "600519")])
def test_actual_stock_list_shell_precedence(configured, inherited, expected):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for the actual Actions shell contract")
    analysis = next(step for step in workflow()["jobs"]["analyze"]["steps"] if step["name"] == "执行股票分析")
    shell = analysis["run"].split("# 处理 LITELLM YAML 配置文件", 1)[0]
    result = subprocess.run(["bash", "-c", shell + "\nprintf '%s' \"$STOCK_LIST\""], env={**os.environ, "STOCK_LIST_CONFIG": configured, "STOCK_LIST": inherited}, text=True, capture_output=True, check=True)
    assert result.stdout == expected


@pytest.mark.parametrize("code", ["MSFT", "SPX"])
@pytest.mark.parametrize("state,session", [("POST", "close"), ("REGULAR", "intraday")])
def test_yahoo_stock_and_index_use_matching_regular_prices_and_distinct_postmarket(code, state, session):
    fast = SimpleNamespace(last_price=999, regular_market_previous_close=990, open=998, day_high=1000, day_low=997, last_volume=1000, market_cap=1000000)
    info = {"shortName": "Synthetic fixture", "currency": "USD", "marketState": state, "regularMarketPrice": 110, "regularMarketPreviousClose": 100, "regularMarketOpen": 101, "regularMarketDayHigh": 112, "regularMarketDayLow": 99, "regularMarketVolume": 1000, "regularMarketTime": REGULAR_TIME, "postMarketPrice": 115 if state == "POST" else None, "postMarketTime": REGULAR_TIME + 7200}
    with patch("yfinance.Ticker", return_value=SimpleNamespace(fast_info=fast, info=info)):
        quote = YfinanceFetcher().get_realtime_quote(code)
    assert quote is not None and quote.source == RealtimeSource.YFINANCE
    assert (quote.price, quote.pre_close, quote.change_pct) == (110, 100, 10)
    assert quote.provider_timestamp == "2026-10-28T20:00:00+00:00" and quote.quote_session == session
    if state == "POST":
        assert quote.extended_price == 115 and quote.extended_session == "postmarket"
        assert quote.to_dict()["extended_timestamp"] == "2026-10-28T22:00:00+00:00"
    else:
        assert quote.extended_price is None


@pytest.mark.parametrize("code,language,unit", [("MSFT", "zh", " USD"), ("MSFT", "en", " USD"), ("600519", "zh", " 元"), ("600519", "en", "")])
def test_price_prompt_currency_preserves_market_and_language_contracts(code, language, unit):
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    context = {"code": code, "stock_name": "Synthetic fixture", "date": "2026-10-28", "today": {"close": 110}}
    with patch.object(analyzer, "_get_skill_prompt_sections", return_value=("", "", True)):
        prompt = analyzer._format_prompt(context, "Synthetic fixture", report_language=language)
    assert f" | 110{unit} |" in prompt
    if code == "MSFT":
        assert "110 元" not in prompt


def test_untimed_yahoo_fallback_is_not_certified_as_realtime():
    quote = _with_yahoo_quote_metadata(UnifiedRealtimeQuote(code="MSFT", market="us", price=110, pre_close=100), {})
    assert quote.provider_timestamp is None and quote.quote_session == "daily"
    assert quote.data_quality == "partial" and "provider_timestamp" in quote.missing_fields
    result = make_result()
    result.market_snapshot["provider_timestamp"] = None
    assert "不能确认实时性" in "\n".join(market_snapshot_notes(result.market_snapshot))


def test_yahoo_missing_regular_previous_close_never_borrows_extended_baseline():
    quote = _with_yahoo_quote_metadata(UnifiedRealtimeQuote(code="MSFT", market="us", price=999, pre_close=990), {"regularMarketPrice": 110, "regularMarketTime": REGULAR_TIME})
    assert quote.pre_close is None and quote.change_pct is None


def test_pipeline_to_report_uses_provider_baseline_and_preserves_metadata():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(report_language="zh")
    pipeline.search_service = SimpleNamespace(news_window_days=3)
    pipeline.fetcher_manager = MagicMock()
    trend = MagicMock(ma5=105, ma10=102, ma20=100)
    quote = UnifiedRealtimeQuote(code="MSFT", source=RealtimeSource.YFINANCE, market="us", currency="USD", price=110, pre_close=100, change_pct=10, open_price=101, high=112, low=99, provider_timestamp="2026-10-28T20:00:00+00:00", quote_session="close")
    context = pipeline._enhance_context({"code": "MSFT", "date": "2026-10-27", "today": {"close": 98}, "yesterday": {"close": 80}}, quote, None, trend, fundamental_context={})
    snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(context)
    assert snapshot["prev_close"] == "100.00" and snapshot["pct_chg"] == "10.00%"
    assert snapshot["currency"] == "USD" and snapshot["provider_timestamp"] == quote.provider_timestamp
    assert snapshot["date"] == "2026-10-28"


def test_snapshot_missing_baseline_is_na_and_daily_math_is_consistent():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    missing = analyzer._build_market_snapshot({"code": "MSFT", "today": {"close": 110, "data_source": "realtime:yfinance"}, "yesterday": {"close": 80}})
    assert missing["prev_close"] == "N/A" and missing["pct_chg"] == "N/A"
    daily = analyzer._build_market_snapshot({"code": "MSFT", "today": {"close": 90, "pct_chg": 25}, "yesterday": {"close": 100}})
    assert daily["pct_chg"] == "-10.00%"


def test_default_and_jinja_report_paths_share_currency_and_quote_disclosures():
    result = make_result()
    original_points = result.dashboard["battle_plan"]["sniper_points"].copy()
    normalized = normalize_price_result(result)
    assert normalized.dashboard["battle_plan"]["sniper_points"]["stop_loss"] == "90 USD"
    assert result.dashboard["battle_plan"]["sniper_points"] == original_points
    for report in (legacy_report([result]), render("markdown", [result])):
        assert report and "USD" in report and "90元" not in report
        assert "行情时间" in report and "获取时间" in report and "独立盘后报价" in report
        assert "20:00:00+00:00" in report and "22:00:00+00:00" in report
        assert "数据质量" in report and "10.00%" in report
    result.dashboard["core_conclusion"]["position_advice"]["has_position"] = "人民币5元，股价90元"
    assert normalize_price_result(result).dashboard["core_conclusion"]["position_advice"]["has_position"] == "人民币5元，股价90 USD"


def test_summary_has_every_requested_symbol_and_excludes_private_payloads(tmp_path):
    result = make_result()
    result.raw_response = "PRIVATE_RAW_SENTINEL"
    result.fundamental_context = {"account_balance": "PRIVATE_BALANCE_SENTINEL"}
    result.portfolio_context = {"quantity": "PRIVATE_QUANTITY_SENTINEL", "average_cost": "PRIVATE_COST_SENTINEL"}
    result.market_snapshot["api_key"] = "PRIVATE_KEY_SENTINEL"
    result.dashboard["battle_plan"]["sniper_points"]["account_balance"] = "PRIVATE_NESTED_SENTINEL"
    path = tmp_path / "report_20261028.md"
    path.write_text("# Synthetic report\n", encoding="utf-8")
    summary = write_analysis_summary(path, [result], UNIVERSE, {code: "429 PRIVATE_ERROR_SENTINEL" for code in UNIVERSE if code != result.code})
    text = path.with_suffix(".json").read_text(encoding="utf-8")
    assert "PRIVATE_" not in text and "raw_response" not in text and "portfolio_context" not in text
    assert path.read_text(encoding="utf-8").startswith("# Synthetic report\n")
    assert summary["counts"] == {"requested": 36, "analyzed": 1, "failed": 35}
    assert len(summary["stocks"]) == 36 and len(set(summary["requested_codes"])) == 36
    assert all(stock["failure_reason"] == "rate_limited" and stock["score"] is None for stock in summary["stocks"] if stock["status"] == "failed")


@pytest.mark.parametrize("count,status", [(36, "complete"), (5, "partial"), (0, "partial")])
def test_publisher_preserves_markdown_aliases_and_rejects_incomplete_coverage(tmp_path, count, status):
    write_fixture(tmp_path, count)
    result = publish_reports(tmp_path, publisher_env())
    assert result["status"] == status
    assert (tmp_path / "report_20261028.md").is_file()
    assert all((tmp_path / "latest" / name).is_file() for name in ("report.md", "summary.json", "market_review.md", "run.json"))
    if count < 36:
        assert "stock_coverage_incomplete" in result["issues"]


@pytest.mark.parametrize("mode,missing_file", [("market-only", "report_20261028.md"), ("stocks-only", "market_review_20261028.md")])
def test_publisher_honors_manual_modes(tmp_path, mode, missing_file):
    write_fixture(tmp_path)
    (tmp_path / missing_file).unlink()
    assert publish_reports(tmp_path, publisher_env(ANALYSIS_MODE=mode))["status"] == "complete"


def test_holiday_and_force_run_reuse_existing_calendar_filter(tmp_path):
    env = publisher_env(TRADING_DAY_CHECK_ENABLED="true")
    with patch("src.core.trading_calendar.get_open_markets_today", return_value=set()):
        assert publish_reports(tmp_path, env)["status"] == "skipped"
        forced = publish_reports(tmp_path, {**env, "FORCE_RUN": "true"})
    assert forced["status"] == "failed" and "stock_report_missing" in forced["issues"]


def test_publisher_rejects_wrong_run_and_tampered_counts(tmp_path):
    write_fixture(tmp_path)
    path = tmp_path / "report_20261028.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["workflow_run_id"] = "old-run"
    summary["counts"]["analyzed"] = 5
    path.write_text(json.dumps(summary), encoding="utf-8")
    result = publish_reports(tmp_path, publisher_env(GITHUB_RUN_ID="new-run"))
    assert "stock_summary_run_mismatch" in result["issues"] and "stock_coverage_incomplete" in result["issues"]
    assert result["status"] != "complete"


def test_report_contract_evidence(tmp_path):
    for name, count in (("complete", 36), ("partial", 5)):
        folder = tmp_path / name
        write_fixture(folder, count)
        publish_reports(folder, publisher_env())
    evidence = os.getenv("REPORT_EVIDENCE_DIR")
    if evidence:
        shutil.copytree(tmp_path, evidence, dirs_exist_ok=True)
        (Path(evidence) / "README.md").write_text("# 合成报告验收样例\n所有价格、信号和事件均为离线合成测试数据，不是实际行情或投资建议。包含默认报告渲染、公开 JSON、完整36只与仅完成5只两种场景。\n", encoding="utf-8")
