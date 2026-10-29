# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Jinja2 Report Renderer
===================================

Renders reports from Jinja2 templates. Falls back to caller's logic on template
missing or render error. Template path is relative to project root.
Any expensive data preparation should be injected by the caller via extra_context.
"""

import logging
import os
import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.analyzer import AnalysisResult
from src.config import get_config
from src.market_context import detect_market
from src.market_phase_summary import format_public_market_status_line, format_public_phase_pack_excerpt, render_market_phase_summary
from src.report_language import (
    get_localized_stock_name,
    get_report_labels,
    get_signal_level,
    get_chip_unavailable_reason,
    is_chip_structure_unavailable,
    localize_chip_health,
    localize_conflict_severity,
    localize_consensus_level,
    localize_operation_advice,
    localize_strategy_signal,
    localize_strategy_skill,
    localize_strategy_conflict_description,
    localize_strategy_synthesis_summary,
    localize_trend_prediction,
    normalize_report_language,
    normalize_strategy_synthesis_payload,
    strategy_invalid_opinion_count,
)
from src.schemas.decision_action import (
    display_action_fields_for_result,
    display_decision_type_for_result,
    display_operation_advice_for_result,
    localize_action_label,
)
from src.utils.data_processing import (
    normalize_model_used,
    signal_attribution_has_content,
    signal_attribution_weight_items,
)

logger = logging.getLogger(__name__)


def _escape_md(text: str) -> str:
    """Escape markdown special chars (*ST etc)."""
    if not text:
        return ""
    return text.replace("*", "\\*").replace("_", "\\_")


def _clean_sniper_value(val: Any) -> str:
    """Format sniper point value for display (strip label prefixes)."""
    if val is None:
        return "N/A"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).strip() if val else ""
    if not s or s == "N/A":
        return s or "N/A"
    prefixes = [
        "理想买入点：", "次优买入点：", "止损位：", "目标位：",
        "理想买入点:", "次优买入点:", "止损位:", "目标位:",
        "Ideal Entry:", "Secondary Entry:", "Stop Loss:", "Target:",
    ]
    for prefix in prefixes:
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _resolve_templates_dir() -> Path:
    """Resolve template directory relative to project root."""
    config = get_config()
    base = Path(__file__).resolve().parent.parent.parent
    templates_dir = Path(config.report_templates_dir)
    if not templates_dir.is_absolute():
        return base / templates_dir
    return templates_dir


from src.services.empty_news import empty_news_disclosure


def render(
    platform: str,
    results: List[AnalysisResult],
    report_date: Optional[str] = None,
    summary_only: bool = False,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Render report using Jinja2 template.

    Args:
        platform: One of: markdown, wechat, brief
        results: List of AnalysisResult
        report_date: Report date string (default: today)
        summary_only: Whether to output summary only
        extra_context: Additional template context

    Returns:
        Rendered string, or None on error (caller should fallback).
    """
    from datetime import datetime, timezone

    results = [normalize_price_result(result) for result in results]
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError:
        logger.warning("jinja2 not installed, report renderer disabled")
        return None

    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    templates_dir = _resolve_templates_dir()
    template_name = f"report_{platform}.j2"
    template_path = templates_dir / template_name
    if not template_path.exists():
        logger.debug("Report template not found: %s", template_path)
        return None

    report_language = normalize_report_language(
        (extra_context or {}).get("report_language")
        or next(
            (getattr(result, "report_language", None) for result in results if getattr(result, "report_language", None)),
            None,
        )
        or getattr(get_config(), "report_language", "zh")
    )
    labels = get_report_labels(report_language)

    # Build template context with pre-computed signal levels (sorted by score)
    sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)
    sorted_enriched = []
    for r in sorted_results:
        display_action = display_action_fields_for_result(
            r,
            report_language=report_language,
        )["action"]
        display_advice = display_operation_advice_for_result(
            r,
            report_language=report_language,
        )
        signal_action = {
            "buy": "buy",
            "add": "buy",
            "hold": "hold",
            "reduce": "reduce",
            "sell": "sell",
            "watch": "watch",
            "avoid": "hold",
            "alert": "sell",
        }.get(display_action, display_action)
        _, se, _ = get_signal_level(signal_action or display_advice, r.sentiment_score, report_language)
        rn = get_localized_stock_name(r.name, r.code, report_language)
        sorted_enriched.append({
            "result": r,
            "empty_news_disclosure": empty_news_disclosure(r, report_language),
            "signal_text": display_advice,
            "signal_emoji": se,
            "stock_name": _escape_md(rn),
            "localized_operation_advice": display_advice,
            "localized_trend_prediction": localize_trend_prediction(r.trend_prediction, report_language),
        })

    display_buckets = [
        display_decision_type_for_result(r, report_language=report_language)
        for r in results
    ]
    buy_count = sum(1 for bucket in display_buckets if bucket == "buy")
    sell_count = sum(1 for bucket in display_buckets if bucket == "sell")
    hold_count = len(display_buckets) - buy_count - sell_count
    show_llm_model = bool(getattr(get_config(), "report_show_llm_model", True))
    models_used: List[str] = []
    if show_llm_model:
        for result in results:
            model = normalize_model_used(getattr(result, "model_used", None))
            if model:
                models_used.append(model)
        models_used = list(dict.fromkeys(models_used))

    report_timestamp = datetime.now(timezone.utc).isoformat()

    def failed_checks(checklist: List[str]) -> List[str]:
        return [c for c in (checklist or []) if c.startswith("❌") or c.startswith("⚠️")]

    def phase_pack_excerpt(result: AnalysisResult) -> str:
        return format_public_phase_pack_excerpt(
            getattr(result, "market_phase_summary", None),
            getattr(result, "analysis_context_pack_overview", None),
            source=getattr(result, "analysis_visibility_source", None) or "evaluator_snapshot",
            report_language=report_language,
        )

    def market_status_line() -> str:
        for source_results in (results or [], sorted_results):
            for result in source_results:
                line = format_public_market_status_line(
                    getattr(result, "market_phase_summary", None),
                    report_language=report_language,
                )
                if line:
                    return line
        return ""

    context: Dict[str, Any] = {
        "report_date": report_date,
        "report_timestamp": report_timestamp,
        "results": sorted_results,
        "enriched": sorted_enriched,  # Sorted by sentiment_score desc
        "summary_only": summary_only,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "hold_count": hold_count,
        "labels": labels,
        "report_language": report_language,
        "models_used": models_used,
        "show_llm_model": show_llm_model,
        "market_status_line": market_status_line(),
        "escape_md": _escape_md,
        "clean_sniper": _clean_sniper_value,
        "failed_checks": failed_checks,
        "phase_pack_excerpt": phase_pack_excerpt,
        "market_snapshot_notes": market_snapshot_notes,
        "history_by_code": {},
        "get_chip_unavailable_reason": get_chip_unavailable_reason,
        "is_chip_structure_unavailable": is_chip_structure_unavailable,
        "localize_operation_advice": localize_operation_advice,
        "localize_action_label": localize_action_label,
        "localize_trend_prediction": localize_trend_prediction,
        "localize_chip_health": localize_chip_health,
        "localize_strategy_signal": localize_strategy_signal,
        "localize_strategy_skill": localize_strategy_skill,
        "localize_strategy_conflict_description": localize_strategy_conflict_description,
        "localize_strategy_synthesis_summary": localize_strategy_synthesis_summary,
        "localize_consensus_level": localize_consensus_level,
        "localize_conflict_severity": localize_conflict_severity,
        "normalize_strategy_synthesis_payload": normalize_strategy_synthesis_payload,
        "strategy_invalid_opinion_count": strategy_invalid_opinion_count,
        "signal_attribution_has_content": signal_attribution_has_content,
        "signal_attribution_weight_items": signal_attribution_weight_items,
    }
    if extra_context:
        safe_extra_context = dict(extra_context)
        safe_extra_context.pop("labels", None)
        safe_extra_context.pop("report_language", None)
        context.update(safe_extra_context)

    try:
        env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(default=False),
        )
        template = env.get_template(template_name)
        return template.render(**context)
    except Exception as e:
        logger.warning("Report render failed for %s: %s", template_name, e)
        return None


def normalize_price_result(result: AnalysisResult) -> AnalysisResult:
    """Normalize unqualified yuan only in stock-price fields, preserving explicit CNY facts."""
    snapshot = getattr(result, "market_snapshot", None) or {}
    currency = snapshot.get("currency") or {"us": "USD", "hk": "HKD"}.get(detect_market(result.code))
    if currency not in {"USD", "HKD"}:
        return result
    result = copy.copy(result)
    result.dashboard = copy.deepcopy(getattr(result, "dashboard", None) or {})

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, str):
            return value

        def replace(match):
            prefix = value[max(0, match.start() - 8):match.start()].rstrip()
            return match.group(0) if prefix.endswith(("人民币", "人民幣", "CNY", "RMB")) else match.group(1) + " " + currency

        return re.sub(r"(?<![\d.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*元", replace, value)

    for section, field in (
        ("core_conclusion", "position_advice"), ("battle_plan", "sniper_points"),
        ("data_perspective", "price_position"), ("phase_decision", "watch_conditions"),
    ):
        container = result.dashboard.get(section)
        if isinstance(container, dict) and field in container:
            container[field] = normalize(container[field])
    return result


def classify_analysis_failure(error: Any) -> str:
    """Export categorical failure codes, never provider exception bodies or credentials."""
    text = str(error or "").lower()
    if any(marker in text for marker in ("429", "quota", "ratelimit", "rate limit")):
        return "rate_limited"
    if any(marker in text for marker in ("503", "serviceunavailable", "high demand")):
        return "provider_unavailable"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "analysis_failed"


def write_analysis_summary(report_path, results, requested_codes, failures):
    """Add coverage to the existing Markdown and a public, allowlisted JSON sidecar."""
    from datetime import datetime, timezone

    path = Path(report_path)
    generated_at = datetime.now(timezone.utc).isoformat()
    requested = list(dict.fromkeys(str(code) for code in requested_codes))
    by_code = {str(result.code): normalize_price_result(result) for result in results if result.success}
    analyzed = [code for code in requested if code in by_code]
    failed = [code for code in requested if code not in by_code]
    snapshot_keys = (
        "date", "analysis_time", "currency", "close", "prev_close", "open", "high", "low",
        "pct_chg", "change_amount", "amplitude", "volume", "amount", "price", "source",
        "daily_source", "daily_is_estimated", "fetched_at", "provider_timestamp", "quote_session",
        "extended_price", "extended_timestamp", "extended_session", "data_quality",
        "missing_fields", "is_stale", "stale_seconds",
    )
    allowed_reasons = {"rate_limited", "provider_unavailable", "timeout", "analysis_failed", "no_result"}
    failures = {code: reason if reason in allowed_reasons else classify_analysis_failure(reason) for code, reason in failures.items()}
    stocks = []
    for code in requested:
        result = by_code.get(code)
        if result is None:
            stocks.append({"code": code, "status": "failed", "failure_reason": failures.get(code, "no_result"), "score": None})
            continue
        dashboard = result.dashboard or {}
        intelligence = dashboard.get("intelligence") or {}
        phase_decision = dashboard.get("phase_decision") or {}
        snapshot = result.market_snapshot or {}
        stocks.append({
            "code": code, "status": "analyzed", "analysis_time": snapshot.get("analysis_time"),
            "score": result.sentiment_score,
            "signal": display_operation_advice_for_result(result, report_language=result.report_language),
            "technical_signals": {
                "trend": result.trend_prediction, "analysis": result.technical_analysis,
                "moving_averages": result.ma_analysis,
            },
            "quote": {key: snapshot.get(key) for key in snapshot_keys},
            "market_phase": render_market_phase_summary(getattr(result, "market_phase_summary", None)),
            "risks": {"warning": result.risk_warning, "alerts": intelligence.get("risk_alerts") or []},
            "catalysts": intelligence.get("positive_catalysts") or [],
            "trigger_conditions": phase_decision.get("watch_conditions") or [],
            "price_levels": {key: ((dashboard.get("battle_plan") or {}).get("sniper_points") or {}).get(key)
                             for key in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")},
            "data_quality": {"sources": result.data_sources, "limitations": phase_decision.get("data_limitations") or []},
        })
    summary = {
        "schema_version": 1, "generated_at": generated_at,
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "status": "complete" if requested and not failed else "partial" if analyzed else "failed",
        "requested_codes": requested, "analyzed_codes": analyzed, "failed_codes": failed,
        "counts": {"requested": len(requested), "analyzed": len(analyzed), "failed": len(failed)},
        "stocks": stocks,
        "disclosure": "Coverage is not market-price certification. Missing quote timestamps are unknown; fetch times are not quote times.",
    }
    language = normalize_report_language(next((result.report_language for result in results), getattr(get_config(), "report_language", "zh")))
    if language == "zh":
        lines = ["## 股票池覆盖", "", f"> 状态：{summary['status']}；计划 {len(requested)} 只，完成 {len(analyzed)} 只，失败 {len(failed)} 只。", f"> 分析汇总时间（UTC）：{generated_at}。覆盖完整不代表行情已独立核实。"]
        if failed:
            lines += ["", "| 未完成代码 | 原因 |", "|---|---|"]
    else:
        lines = ["## Universe coverage", "", f"> Status: {summary['status']}; requested {len(requested)}, analyzed {len(analyzed)}, failed {len(failed)}.", f"> Summary time (UTC): {generated_at}. Coverage is not independent market-price verification."]
        if failed:
            lines += ["", "| Failed code | Reason |", "|---|---|"]
    lines += [f"| {code} | {failures.get(code, 'no_result')} |" for code in failed]
    content = path.read_text(encoding="utf-8")
    path.write_text("\n".join(lines) + "\n\n---\n\n" + content, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def market_snapshot_notes(snapshot, report_language="zh"):
    """One disclosure contract for legacy, single-stock and Jinja report paths."""
    english = report_language in {"en", "ko"}
    unknown = "unknown" if english else "未知"
    stamp = snapshot.get("provider_timestamp")
    fetched = snapshot.get("fetched_at") or unknown
    currency = snapshot.get("currency") or unknown
    session = snapshot.get("quote_session") or "unknown"
    missing = ", ".join(snapshot.get("missing_fields") or []) or ("none reported" if english else "未报告")
    if english:
        notes = [f"> Analysis time (UTC): {snapshot.get('analysis_time') or unknown}; bar date: {snapshot.get('date') or unknown}; bar source: {snapshot.get('daily_source') or unknown}; estimated: {bool(snapshot.get('daily_is_estimated'))}."]
        if "price" in snapshot:
            notes.append(f"> Quote session: {session}; provider time: {stamp or 'unknown — real-time status unverified'}; fetched at: {fetched}.")
        if snapshot.get("extended_price") is not None and snapshot.get("extended_timestamp"):
            notes.append(f"> Separate {snapshot.get('extended_session')} quote: {snapshot['extended_price']} {currency}; provider time: {snapshot['extended_timestamp']}. Not used as the regular close.")
        notes.append(f"> Data quality: {snapshot.get('data_quality') or unknown}; missing: {missing}; stale: {snapshot.get('is_stale')}. Fetch time is not quote time; table arithmetic does not independently verify market prices.")
    else:
        sessions = {"intraday": "盘中（未收盘）", "close": "常规时段收盘报价", "daily": "日线数据", "unknown": "未知"}
        notes = [f"> 分析时间（UTC）：{snapshot.get('analysis_time') or unknown}；行情日期：{snapshot.get('date') or unknown}；日线来源：{snapshot.get('daily_source') or unknown}；估算数据：{bool(snapshot.get('daily_is_estimated'))}。"]
        if "price" in snapshot:
            quote_time = stamp or "未知（不能确认实时性）"
            notes.append(f"> 报价时段：{sessions.get(session, unknown)}；行情时间：{quote_time}；获取时间：{fetched}。")
        if snapshot.get("extended_price") is not None and snapshot.get("extended_timestamp"):
            extended_session = "盘后" if snapshot.get("extended_session") == "postmarket" else "盘前"
            notes.append(f"> 独立{extended_session}报价：{snapshot['extended_price']} {currency}；行情时间：{snapshot['extended_timestamp']}。此价格不作为常规时段收盘价。")
        notes.append(f"> 数据质量：{snapshot.get('data_quality') or unknown}；缺失字段：{missing}；陈旧标记：{snapshot.get('is_stale')}。获取时间不等于行情时间，表内涨跌幅算术一致也不代表市场价格已独立核实。")
    return notes
