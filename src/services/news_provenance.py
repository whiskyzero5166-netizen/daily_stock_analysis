"""Public search provenance and conservative financial-evidence handling.

Search snippets and AI summaries are evidence leads, never independent factual
verification. This module performs no HTTP requests or additional searches.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Publisher domains, not an analysis watchlist. Unknown issuers remain
# unverified rather than guessing an investor-relations domain from a ticker.
PRIMARY_DOMAINS = frozenset({
    "sec.gov", "microsoft.com", "marvell.com", "servicenow.com", "tsmc.com",
    "arista.com", "amazon.com", "aboutamazon.com", "abc.xyz", "thermofisher.com",
    "regeneron.com", "hubbell.com", "constellationenergy.com", "tranetechnologies.com",
    "quantaservices.com", "gevernova.com", "sse.com.cn", "szse.cn", "cninfo.com.cn", "hkexnews.hk",
})
MATERIAL_TERMS = {
    "guidance": r"guidance|outlook|指引|业绩预告|展望",
    "earnings": r"earnings|quarterly results|\bEPS\b|财报|季度业绩|每股收益",
    "merger": r"merger|acquisition|并购|收购|合并交易",
    "filing": r"\b(?:10-[KQ]|8-K|20-F|6-K)\b|SEC filing|监管披露",
    "consensus": r"consensus|price target|分析师一致预期|目标价",
    "valuation": r"valuation|forward P/E|估值|市盈率",
}


def _url_parts(url):
    try:
        return urlsplit(str(url or ""))
    except ValueError:
        return urlsplit("")


def primary_source(url):
    host = (_url_parts(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in PRIMARY_DOMAINS)


def issuer_key(url):
    parsed = _url_parts(url)
    host = (parsed.hostname or "").lower()
    if host == "sec.gov" or host.endswith(".sec.gov"):
        cik = re.search(r"/edgar/data/(\d+)/", parsed.path, re.I)
        return "sec-cik:" + cik.group(1).lstrip("0") if cik else None
    return next((domain for domain in PRIMARY_DOMAINS
                 if host == domain or host.endswith("." + domain)), None)


def canonical_news_url(url):
    parsed = _url_parts(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if any(re.search(r"api[_-]?key|token|secret|password|authorization", key, re.I) for key, _ in parse_qsl(parsed.query)):
        return ""
    query = [(key, value) for key, value in parse_qsl(parsed.query)
             if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


def deduplicate_news(items):
    """Keep original URLs/timestamps and combine the actual supplying providers."""
    result, by_key = [], {}
    for item in items:
        key = canonical_news_url(item.url) or (str(item.title).strip().casefold(), str(item.published_date))
        if key in by_key:
            existing = by_key[key]
            existing.search_providers = list(dict.fromkeys(
                (existing.search_providers or []) + (item.search_providers or [])
                + [provider for provider in (existing.search_provider, item.search_provider) if provider]
            ))
            continue
        by_key[key] = item
        result.append(item)
    return result


def financial_evidence(items, as_of=None):
    """Identify the newest *supplied* dated disclosure, never claim global latest."""
    as_of = as_of or datetime.now(timezone.utc)
    records = []
    for item in deduplicate_news(list(items)):
        text = str(item.title) + " " + str(item.snippet)
        kinds = [kind for kind, pattern in MATERIAL_TERMS.items() if re.search(pattern, text, re.I)]
        if not kinds:
            continue
        raw_date = item.original_published_date or item.published_date
        try:
            published = datetime.fromisoformat(str(item.published_date).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            published = None
        url = str(item.url) if canonical_news_url(item.url) else None
        primary = bool(url and primary_source(url))
        evidence_status = (
            "missing_url" if not url else "missing_date" if published is None else
            "future_date" if published > as_of.date() else
            "dated_primary_source" if primary else "secondary_source"
        )
        records.append({
            "title": str(item.title)[:300], "url": url,
            "publisher": str(item.source)[:100], "search_provider": item.search_provider,
            "issuer_key": issuer_key(url),
            "search_providers": list(item.search_providers or []),
            "published_at": str(raw_date) if raw_date is not None else None,
            "publication_date": published.isoformat() if published else None,
            "retrieved_at": item.retrieved_at, "event_types": kinds,
            "evidence_status": evidence_status, "verification_status": "unverified",
            "verification_reason": "Search text has not been checked against the original disclosure.",
        })
    for record in records:
        if record["evidence_status"] != "dated_primary_source":
            continue
        newer = [candidate for candidate in records
                 if candidate["evidence_status"] == "dated_primary_source"
                 and record["issuer_key"] is not None and candidate["issuer_key"] == record["issuer_key"]
                 and set(candidate["event_types"]) & set(record["event_types"]) & {"guidance", "earnings"}
                 and candidate["publication_date"] > record["publication_date"]]
        if newer:
            newest = max(newer, key=lambda item: item["publication_date"])
            record["superseded_by"] = newest["url"]
    return records


def public_financial_evidence(evidence):
    allowed = {"title", "url", "publisher", "search_provider", "search_providers", "issuer_key",
               "published_at", "publication_date", "retrieved_at", "event_types", "evidence_status",
               "verification_status", "verification_reason", "superseded_by"}
    records = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        record = {key: value for key, value in item.items() if key in allowed}
        for key in ("url", "superseded_by"):
            if record.get(key) and not canonical_news_url(record[key]):
                record[key] = None
        record["verification_status"] = "unverified"
        records.append(record)
    return records


def label_financial_claims(result, evidence):
    """Mark unsupported material assertions without changing scores or signals."""
    if result is None:
        return result
    result.financial_evidence = evidence
    patterns = "|".join(MATERIAL_TERMS.values())

    def label(value):
        if isinstance(value, list):
            return [label(item) for item in value]
        if isinstance(value, dict):
            return {key: label(item) for key, item in value.items()}
        if not isinstance(value, str) or not re.search(patterns, value, re.I):
            return value
        if value.startswith("[Financial fact unverified"):
            return value
        # Even a valid source URL does not prove that a generated numeric
        # assertion matches its body. Suppress unsupported 'latest' wording.
        value = re.sub(r"最新(?=.{0,12}(?:指引|财报|业绩))", "检索材料中的", value)
        value = re.sub(r"\blatest(?=\s+(?:guidance|earnings|outlook))", "available", value, flags=re.I)
        return "[Financial fact unverified / 财务事实未独立核实] " + value

    for key in ("analysis_summary", "fundamental_analysis", "news_summary", "risk_warning", "dashboard"):
        if hasattr(result, key):
            setattr(result, key, label(getattr(result, key)))
    return result
