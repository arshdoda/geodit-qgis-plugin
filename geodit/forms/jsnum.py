"""JavaScript number semantics the ported runtime depends on.

The web runtime leans on ``Number(…)`` coercion, ``Math.round`` and
``String(number)`` in places where Python differs (``float("0x10")`` fails,
``round(2.5)`` is 2, ``str(5.0)`` is ``"5.0"``). These helpers reproduce the
JavaScript results for the value shapes the runtime actually passes, so a
ported comparison or computed default gives the same answer as the web.
"""

from __future__ import annotations

import math
import re
from typing import Any, Tuple

NAN = float("nan")

_DECIMAL = re.compile(r"^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_RADIX = {"0x": 16, "0o": 8, "0b": 2}
_RADIX_DIGITS = {16: re.compile(r"^[0-9a-fA-F]+$"), 8: re.compile(r"^[0-7]+$"), 2: re.compile(r"^[01]+$")}
# `String.prototype.trim` / `Number()` whitespace: ASCII space and tabs plus the
# Unicode spaces and line terminators Python's `str.strip()` also removes.
_JS_SPACE = " \t\n\v\f\r                 　﻿"


def is_number(value: Any) -> bool:
    """``typeof value === "number"`` (a bool is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def as_float(value: Any) -> float:
    """``float(number)``, with an int too large for a double read as ±Infinity
    (a JavaScript number can't hold more; 400 typed digits are Infinity there)."""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def js_trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def js_number(value: Any) -> float:
    """``Number(value)``. ``None`` reads as ``undefined`` (NaN)."""
    if value is None:
        return NAN
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if is_number(value):
        return as_float(value)
    if isinstance(value, str):
        text = js_trim(value)
        if text == "":
            return 0.0
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        prefix = text[:2].lower()
        if prefix in _RADIX:
            base = _RADIX[prefix]
            digits = text[2:]
            return as_float(int(digits, base)) if _RADIX_DIGITS[base].match(digits) else NAN
        if _DECIMAL.match(text):
            try:
                return float(text)
            except (OverflowError, ValueError):  # pragma: no cover - the regex admits only valid literals
                return NAN
        return NAN
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return 0.0
        if len(value) == 1:
            inner = value[0]
            return js_number(js_str(inner) if is_number(inner) else ("" if inner is None else inner))
        return NAN
    return NAN


def js_number_of_null(value: Any) -> float:
    """``Number(value)`` where ``None`` is JSON ``null`` (0), not ``undefined``."""
    return 0.0 if value is None else js_number(value)


def is_finite(value: Any) -> bool:
    """``Number.isFinite(value)`` — no coercion: only a finite number passes."""
    return is_number(value) and math.isfinite(as_float(value))


def is_integer(value: Any) -> bool:
    """``Number.isInteger(value)``."""
    return is_finite(value) and as_float(value).is_integer()


def js_round(value: float) -> Any:
    """``Math.round``: halves round up (towards +∞), unlike Python's ``round``;
    an int for a finite value, and ±Infinity / NaN back unchanged. Exact where
    ``floor(x + 0.5)`` isn't: above 2**52 that sum rounds to even, and
    0.49999999999999994 + 0.5 is 1."""
    number = as_float(value)
    if not math.isfinite(number):
        return number
    floor = math.floor(number)
    return int(floor) + (1 if number - floor >= 0.5 else 0)


def as_int_if_integral(value: float) -> Any:
    """A float that holds a whole number as an ``int`` (``5.0`` → ``5``), so it
    serializes and compares the way a JavaScript number does."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and abs(value) < 2**63:
        return int(value)
    return value


def _shortest(value: float) -> Tuple[str, int]:
    """Shortest round-trip digits of ``value`` (> 0) and ``n`` such that
    ``value == 0.<digits> × 10**n`` — the ``k`` / ``n`` of ECMA-262 Number::toString."""
    text = repr(value).lower()
    mantissa, _, exp = text.partition("e")
    exponent = int(exp) if exp else 0
    whole, _, fraction = mantissa.partition(".")
    raw = whole + fraction
    stripped = raw.lstrip("0")
    point = len(whole) + exponent - (len(raw) - len(stripped))
    digits = stripped.rstrip("0") or "0"
    return digits, point


def js_str(value: Any) -> str:
    """``String(value)`` for numbers (and the scalars the runtime stringifies)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        if abs(value) < 10**21:
            return str(value)
        value = as_float(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, n = _shortest(abs(value))
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    exp = n - 1
    exp_text = ("+" if exp >= 0 else "-") + str(abs(exp))
    if k == 1:
        return sign + digits + "e" + exp_text
    return sign + digits[0] + "." + digits[1:] + "e" + exp_text
