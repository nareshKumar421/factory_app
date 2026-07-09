# quality_control/services/spec_evaluation.py
"""
Auto-evaluate whether a QC result is within spec.

QC specs are entered by hand as free text in ``QCParameterMaster.standard_value``
and, in practice, the structured ``min_value`` / ``max_value`` columns are almost
never filled. Likewise inspectors type the measured value into the text field
``InspectionParameterResult.result_value`` (e.g. ``"230"``, ``"AVG-233"``) rather
than the numeric ``result_numeric`` column.

This module parses both sides so ``is_within_spec`` can be derived automatically:

Spec range notations seen in real data (all case-insensitive):
  * Tolerance:   ``235+-5.0``, ``17.00 ± 0.15``, ``10.00 +/- 1.00``, ``155±1.0``
  * Comparator:  ``NLT 20 Kgf`` (min), ``NMT 0.5`` / ``Max. 125`` (max)
  * Dash range:  ``58.6-61.7``, ``1.4620-1.4640`` (optionally with a ``@40`` suffix)
  * Descriptive: ``Free from visual defects`` -> not numeric, returns None

When the spec is not numeric (visual / pass-fail checks) the caller keeps the
pass/fail flag the inspector entered by hand.
"""

import re
from decimal import Decimal, InvalidOperation

# A single unsigned number, allowing a leading dot (".05") — e.g. 235, 5.0, .05.
_NUMBER = r"\d*\.?\d+"

# Value ± tolerance, after normalizing "+/-" and "+-" to "±".
_TOLERANCE_RE = re.compile(rf"({_NUMBER})\s*±\s*({_NUMBER})")

# One-sided bounds. Keywords are matched as whole words so "min" inside a word
# like "aluminium" can't trigger a false bound.
_UPPER_RE = re.compile(rf"(?:\bNMT\b|\bNOT\s+MORE\s+THAN\b|\bMAX\b|<=|<)\D*({_NUMBER})", re.IGNORECASE)
_LOWER_RE = re.compile(rf"(?:\bNLT\b|\bNOT\s+LESS\s+THAN\b|\bMIN\b|>=|>)\D*({_NUMBER})", re.IGNORECASE)

# "A - B" range anchored at the start of the string.
_DASH_RANGE_RE = re.compile(rf"^\s*({_NUMBER})\s*-\s*({_NUMBER})")

# First unsigned number anywhere (used to pull a reading out of result text).
_FIRST_NUMBER_RE = re.compile(_NUMBER)


def _to_decimal(token):
    try:
        return Decimal(token)
    except (InvalidOperation, TypeError):
        return None


def _normalize_tolerance(text):
    """Fold the various 'plus/minus' spellings to a single ± sign."""
    text = text.replace("_", " ")
    text = text.replace("+/-", "±").replace("+ /-", "±")
    text = text.replace("+ -", "±").replace("+-", "±")
    return text


def extract_result_number(result_value):
    """Pull the measured number out of a free-text result, or None.

    Takes the first unsigned number so labelled entries like ``"AVG-233"`` or
    ``"AVG. 232"`` still yield ``233`` / ``232``.
    """
    if not result_value:
        return None
    match = _FIRST_NUMBER_RE.search(result_value)
    return _to_decimal(match.group(0)) if match else None


def parse_spec_range(standard_value):
    """Parse a spec string into ``(low, high)`` Decimals, or None if not numeric.

    Either bound may be None for open-ended comparator specs (e.g. ``NLT 20`` ->
    ``(20, None)``). For specs listing more than one tolerance the first is used.
    """
    if not standard_value:
        return None

    normalized = _normalize_tolerance(standard_value)

    tol = _TOLERANCE_RE.search(normalized)
    if tol:
        center = _to_decimal(tol.group(1))
        spread = _to_decimal(tol.group(2))
        if center is not None and spread is not None:
            spread = abs(spread)
            return center - spread, center + spread

    low = high = None
    upper = _UPPER_RE.search(standard_value)
    if upper:
        high = _to_decimal(upper.group(1))
    lower = _LOWER_RE.search(standard_value)
    if lower:
        low = _to_decimal(lower.group(1))
    if low is not None or high is not None:
        return low, high

    dash = _DASH_RANGE_RE.search(standard_value)
    if dash:
        a = _to_decimal(dash.group(1))
        b = _to_decimal(dash.group(2))
        if a is not None and b is not None and a <= b:
            return a, b

    return None


def evaluate_within_spec(standard_value, min_value, max_value, result_numeric, result_value):
    """Return True/False if the result can be judged against the spec, else None.

    None means "cannot decide automatically" — no numeric reading, or a
    non-numeric (visual / descriptive) spec — and the caller should leave any
    manually entered pass/fail flag untouched.

    Structured ``min_value`` / ``max_value`` take precedence; otherwise the
    bounds are parsed from the ``standard_value`` text.
    """
    measured = result_numeric
    if measured is None:
        measured = extract_result_number(result_value)
    if measured is None:
        return None

    if min_value is not None or max_value is not None:
        low, high = min_value, max_value
    else:
        parsed = parse_spec_range(standard_value)
        if parsed is None:
            return None
        low, high = parsed

    if low is None and high is None:
        return None
    if low is not None and measured < low:
        return False
    if high is not None and measured > high:
        return False
    return True
