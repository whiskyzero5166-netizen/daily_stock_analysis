"""Deterministic arithmetic checks; these do not authenticate market prices."""
import math
import re


def numeric_value(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "").removesuffix("%")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def validate_price_change(quote):
    price = numeric_value(quote.get("price") or quote.get("close"))
    previous = numeric_value(quote.get("prev_close"))
    observed = numeric_value(quote.get("pct_chg"))
    if price is None or previous is None or previous <= 0 or observed is None:
        return {"status": "unverified", "reason": "missing_price_change_basis"}
    expected = (price / previous - 1) * 100
    difference = abs(expected - observed)
    return {"status": "passed" if difference <= 0.1 + 1e-9 else "mismatch",
            "expected_pct": round(expected, 6), "observed_pct": observed,
            "difference_pp": round(difference, 6), "tolerance_pp": 0.1,
            "disclosure": "Table arithmetic only; market prices are not independently certified."}
