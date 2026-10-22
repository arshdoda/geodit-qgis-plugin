"""Per-answer validation.

Port of geodit-ui ``runtime/form-validation.ts`` (``validateValue``,
``isEmptyForQuestion``, ``storableRangeError``, ``visibleOptionsFor``,
``flattenForCalc``) with the same messages, so an answer QGIS accepts is one the
web accepts too, and of the limits it reads from
``services/form/detail/constant.ts`` (``textLengthRange``, ``ratingScale``,
``ratingStarFill``). One deliberate difference: PHONE numbers are checked
against the country calling code and the E.164 length (``phone.py``) — the
web's libphonenumber is not available in QGIS's Python.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Set

from .answers import (
    MANUAL_TEXT_MAX_LEN,
    Answers,
    TabKey,
    coerce_choice_list,
    coerce_numeric,
    coerce_string,
    get_manual_text,
    is_empty_answer,
    is_manual_value,
    js_string_of,
    stored_choice_list_length,
    tab_key,
)
from .defaults import is_schema_unique_id
from .jsnum import as_float, is_integer, is_number, js_number, js_round, js_trim
from .model import (
    MANUAL_ENTRY_OPTION,
    PARAGRAPH_ANSWER_MAX_LEN,
    TEXT_ANSWER_MAX_LEN,
    DefaultType,
    Form,
    IdentityType,
    Option,
    QType,
    Question,
)
from .phone import is_valid_phone
from .rules import EvaluationResult

_AADHAAR = re.compile(r"[0-9]{12}")
_PAN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
_VOTER_ID = re.compile(r"[A-Z]{3}[0-9]{7}")
_PASSPORT = re.compile(r"[A-PR-WYa-pr-wy][0-9]{7}")
_DRIVING_LICENSE = re.compile(r"[A-Z]{2}[0-9]{2}[ -]?(19|20)?[0-9]{2}[0-9]{7}")
_DATE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})(T([0-9]{2}):([0-9]{2})(:([0-9]{2}))?(\.([0-9]+))?(Z|[+-]([0-9]{2}):?([0-9]{2}))?)?"
)
_EMAIL = re.compile(r"[^\s@]+@(?:[^\s@.]+\.)+[A-Za-z]{2,}")
_SPACES = re.compile(r"\s+")
# JavaScript's `\d`: ASCII only (Python's takes other scripts' digits, which int() reads).
_DIGITS = re.compile(r"[0-9]+")

# What each numeric answer column can store: the whole-number columns can't go
# negative, NUMBER is capped at 2^53 − 1, DECIMAL / MEASUREMENT are
# numeric(15,4) (at most 11 digits before the point). Only DECIMAL may be
# negative — a length or area can't. RATING takes a fraction (user decision,
# 2026-10-22): until api-v2 stores it as a decimal, its int4 column truncates
# one (3.5 stores 3), but the range still holds.
INT4_MAX = 2_147_483_647
MAX_SAFE_INTEGER = 9_007_199_254_740_991
WHOLE_NUMBER_MAX = {QType.NUMBER: MAX_SAFE_INTEGER, QType.COUNTER: INT4_MAX}
FRACTION_MAX = {QType.RATING: INT4_MAX}
DECIMAL_QTYPES = frozenset({QType.DECIMAL, QType.MEASUREMENT})
DECIMAL_SCALE = 1e4
DECIMAL_SCALED_LIMIT = 1e15
CHECK_CALC_INPUTS = " — check the answers it's calculated from."
REQUIRED = "This question is required."
# EMAIL, IDENTITY, DROPDOWN and MULTIPLE_CHOICE answers are stored in a
# varchar(255) too: api-v2 measures them in code points (``len(str(value))``)
# and rejects the whole save past it.
CHAR_TOO_LONG = f"Maximum {TEXT_ANSWER_MAX_LEN} characters."
# A RATING question's stars: at most 10 (the builder's cap, and Android's), and
# 5 for one with no scale of its own (api-v2 seeds none).
RATING_SCALE_MAX = 10
RATING_SCALE_DEFAULT = 5


def _js_date_valid(match: re.Match[str]) -> bool:
    """``!Number.isNaN(new Date(value).getTime())`` for a string that passed the
    date pattern — V8's ISO parser: a day up to 31 whatever the month (it rolls
    over), and 24:00 only on the dot."""
    month, day = int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    if match.group(4):
        hour, minute = int(match.group(5)), int(match.group(6))
        second = int(match.group(8) or 0)
        fraction = match.group(10) or ""
        if hour > 24 or minute > 59 or second > 59:
            return False
        if hour == 24 and (minute or second or fraction.strip("0")):
            return False
        if match.group(12) is not None and (int(match.group(12)) > 23 or int(match.group(13)) > 59):
            return False
    return True


def js_len(text: str) -> int:
    """``string.length``: UTF-16 code units, as the web counts characters."""
    return len(text.encode("utf-16-le")) // 2


def format_en_in(number: Any) -> str:
    """``Intl.NumberFormat("en-IN")`` for the whole numbers the messages quote:
    the last three digits, then groups of two (``90,07,19,92,54,74,0991``)."""
    value = js_number(number) if not is_number(number) else float(number)
    if not math.isfinite(value):
        return str(value)
    sign = "-" if value < 0 else ""
    digits = str(int(abs(value)))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    groups: List[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail])


def text_length_range(attributes: Mapping[str, Any]) -> Dict[str, int]:
    """TEXT's Minimum / Maximum characters as the form enforces them."""
    maximum = attributes.get("maximum")
    minimum = attributes.get("minimum")
    max_len = (
        min(int(math.floor(maximum)), TEXT_ANSWER_MAX_LEN)
        if is_number(maximum) and maximum >= 1
        else TEXT_ANSWER_MAX_LEN
    )
    min_len = min(int(math.ceil(minimum)), max_len) if is_number(minimum) and minimum > 0 else 0
    return {"min": min_len, "max": max_len}


def rating_scale(attributes: Mapping[str, Any]) -> int:
    """A RATING question's scale: the stars every client draws and the most a
    rating may be. The author's ``max_rating`` in whole stars (7.5 draws 7), at
    most 10; 0, None or unset means 5. Android's ``parseRatingMaximum`` is the
    same rule."""
    maximum = attributes.get("max_rating")
    if not (is_number(maximum) and maximum > 0):
        return RATING_SCALE_DEFAULT
    # Capped before truncating: an infinite scale is 10, as JavaScript reads it.
    return RATING_SCALE_MAX if maximum >= RATING_SCALE_MAX else max(int(maximum), 1)


def rating_star_fill(value: float, star: int) -> float:
    """How much of star ``star`` (counted from 1) a rating of ``value`` fills,
    from 0 to 1: 3.5 fills half the fourth."""
    return min(1.0, max(0.0, value - (star - 1)))


def is_empty_for_question(question: Question, value: Any) -> bool:
    if is_empty_answer(value):
        return True
    # A manual entry with no typed text counts as empty.
    if question.q_type == QType.DROPDOWN and isinstance(value, str) and is_manual_value(value):
        if js_trim(get_manual_text(value)) == "":
            return True
    if question.q_type == QType.MULTIPLE_CHOICE:
        items = coerce_choice_list(value)
        if not items:
            return True
        if all(is_manual_value(v) for v in items) and all(js_trim(get_manual_text(v)) == "" for v in items):
            return True
    return False


def storable_range_error(question: Question, value: Any) -> Optional[str]:
    """Whether the answer column can store ``value`` at all — never waived, not
    even for a read-only (calculated) question, whose value is still submitted."""
    q_type = question.q_type
    max_whole = WHOLE_NUMBER_MAX.get(q_type)
    max_value = max_whole if max_whole is not None else FRACTION_MAX.get(q_type)
    is_decimal = q_type in DECIMAL_QTYPES
    if max_value is None and not is_decimal:
        return None
    number = coerce_numeric(value)
    if number is None:
        return None
    default_type = question.attr("default_type")
    calculated = (float(default_type) if is_number(default_type) else js_number(default_type)) == DefaultType.CALCULATE
    if max_whole is not None and not float(number).is_integer():
        return "Enter a whole number."
    if number < 0 and q_type != QType.DECIMAL:
        return f"Calculated value can't be negative{CHECK_CALC_INPUTS}" if calculated else "Must be at least 0."
    if max_value is not None and number > max_value:
        cap = format_en_in(max_value)
        return (
            f"Calculated value can't be more than {cap}{CHECK_CALC_INPUTS}" if calculated else f"Must be at most {cap}."
        )
    # Rounded first, as the server does.
    if is_decimal and js_round(abs(number) * DECIMAL_SCALE) >= DECIMAL_SCALED_LIMIT:
        if calculated:
            return f"Calculated value is too large (max 11 digits before the decimal point){CHECK_CALC_INPUTS}"
        return "Use at most 11 digits before the decimal point."
    return None


def _as_number(value: Any) -> float:
    """``typeof value === "number" ? value : Number(value)``."""
    if is_number(value):
        return as_float(value)
    if value is None:
        return 0.0  # Number(null)
    return js_number(value)


def _range_error(attrs: Mapping[str, Any], number: float) -> Optional[str]:
    minimum = attrs.get("minimum")
    maximum = attrs.get("maximum")
    if is_number(minimum) and number < minimum:
        return f"Must be at least {js_string_of(minimum)}."
    if is_number(maximum) and maximum > 0 and number > maximum:
        return f"Must be at most {js_string_of(maximum)}."
    return None


def validate_value(
    question: Question,
    value: Any,
    *,
    read_only_editable: bool = False,
    stored_unchanged: bool = False,
    stored_value: Any = None,
) -> Optional[str]:
    """The error message for one answer, or None. ``read_only_editable`` is the
    answer sheet's "Edit read-only fields": an author-read-only question is then
    checked like any other, except it can never be required.

    ``stored_unchanged``: the response already stores this exact value here, so
    its stored length isn't checked again — the server holds it, and a legacy
    shape can measure longer here than it does there.

    ``stored_value``: what the response stores at this slot, when editing a
    saved one. A choice naming an option the question no longer has (deleted
    after the answer was saved) passes when it is stored here — the whole value
    for DROPDOWN, per item for MULTIPLE_CHOICE, a manual entry included once
    "Enter Manually" is gone. Otherwise every save of that response failed,
    even an edit to another question (FBUILD-24). A new pick is still checked."""
    raw = question.attributes if isinstance(question.attributes, Mapping) else {}
    unlocked = bool(raw.get("read_only")) and read_only_editable
    if raw.get("read_only") and not unlocked:
        return storable_range_error(question, value)
    attrs = {**raw, "mandatory": False} if unlocked and raw.get("mandatory") else raw
    if attrs.get("mandatory") and is_empty_for_question(question, value):
        return REQUIRED
    if is_empty_answer(value):
        return None

    q_type = question.q_type
    if q_type == QType.EMAIL:
        if isinstance(value, str) and not _EMAIL.fullmatch(js_trim(value)):
            return "Enter a valid email address."
        if isinstance(value, str) and not stored_unchanged and len(value) > TEXT_ANSWER_MAX_LEN:
            return CHAR_TOO_LONG
        return None
    if q_type == QType.PHONE:
        if not isinstance(value, str) or not js_trim(value):
            return None
        return None if is_valid_phone(value) else "Enter a valid phone number."
    if q_type == QType.IDENTITY:
        text = js_trim(value) if isinstance(value, str) else ""
        if not text:
            return None
        kind = attrs.get("identity_type")
        if is_number(kind):
            if kind == IdentityType.AADHAAR_CARD and not _AADHAAR.fullmatch(_SPACES.sub("", text)):
                return "Enter a valid 12-digit Aadhaar number."
            if kind == IdentityType.PAN_CARD and not _PAN.fullmatch(text.upper()):
                return "Enter a valid PAN (e.g. ABCPD1234E)."
            if kind == IdentityType.VOTER_ID and not _VOTER_ID.fullmatch(text.upper()):
                return "Enter a valid 10-character Voter ID (e.g. ABC1234567)."
            if kind == IdentityType.PASSPORT and not _PASSPORT.fullmatch(text):
                return "Enter a valid passport number (e.g. A1234567)."
            if kind == IdentityType.DRIVING_LICENSE and not _DRIVING_LICENSE.fullmatch(text.upper()):
                return "Enter a valid driving licence number."
        if not stored_unchanged and len(js_string_of(value)) > TEXT_ANSWER_MAX_LEN:
            return CHAR_TOO_LONG
        return None
    if q_type == QType.ID:
        if is_schema_unique_id(attrs):
            text = js_trim(value) if isinstance(value, str) else ""
            if attrs.get("mandatory") and not text:
                return "Please fill in the referenced questions to generate this id."
            return None
        # api-v2 stores an ID only as a whole number in an int4 column; anything
        # else fails the whole save (RUNTIME-17). The stored value is never
        # re-sent, so a text row an old re-type left behind (RUNTIME-15) doesn't
        # block the save.
        if stored_unchanged:
            return None
        text = js_trim(js_string_of(value))
        number = value if is_number(value) else js_number(text) if _DIGITS.fullmatch(text) else math.nan
        if not is_integer(number) or number < 0:
            return "Enter a whole number."
        if number > INT4_MAX:
            return f"Must be at most {format_en_in(INT4_MAX)}."
        return None
    if q_type == QType.NUMBER:
        number = _as_number(value)
        if not math.isfinite(number):
            return "Enter a number."
        if not number.is_integer():
            return "Enter a whole number."
        return _range_error(attrs, number) or storable_range_error(question, number)
    if q_type in (QType.DECIMAL, QType.MEASUREMENT, QType.COUNTER):
        number = _as_number(value)
        if not math.isfinite(number):
            return "Enter a number."
        return _range_error(attrs, number) or storable_range_error(question, number)
    if q_type == QType.RATING:
        number = _as_number(value)
        if not math.isfinite(number):
            return "Pick a rating."
        scale = rating_scale(attrs)
        if number < 1 or number > scale:
            return f"Pick a rating between 1 and {scale}."
        return storable_range_error(question, number)
    if q_type == QType.TEXT:
        if not isinstance(value, str):
            return None
        length = js_len(js_trim(value))
        limits = text_length_range(attrs)
        if limits["min"] > 0 and length < limits["min"]:
            return f"Must be at least {limits['min']} characters."
        if length > limits["max"]:
            return f"Maximum {limits['max']} characters."
        return None
    if q_type == QType.PARAGRAPH:
        if not isinstance(value, str):
            return None
        if js_len(js_trim(value)) > PARAGRAPH_ANSWER_MAX_LEN:
            return f"Maximum {format_en_in(PARAGRAPH_ANSWER_MAX_LEN)} characters."
        return None
    if q_type == QType.DATETIME:
        match = _DATE.fullmatch(value) if isinstance(value, str) else None
        if match is None or not _js_date_valid(match):
            return "Pick a valid date."
        return None
    if q_type == QType.DROPDOWN:
        options = question.opt_list
        ident = value if isinstance(value, str) else js_string_of(value) if value is not None else ""
        if not ident:
            return None
        has_manual = any(o.value == MANUAL_ENTRY_OPTION for o in options)
        kept = stored_value is not None and coerce_string(stored_value) == ident
        if is_manual_value(ident):
            if not has_manual and not kept:
                return "Select a valid option."
            if attrs.get("mandatory") and js_trim(get_manual_text(ident)) == "":
                return "Type your manual answer."
            if not stored_unchanged and len(get_manual_text(ident)) > MANUAL_TEXT_MAX_LEN:
                return f"Maximum {MANUAL_TEXT_MAX_LEN} characters."
            return None
        if not any(str(o.id) == ident for o in options) and not kept:
            return "Select a valid option."
        return None
    if q_type == QType.MULTIPLE_CHOICE:
        options = question.opt_list
        has_manual = any(o.value == MANUAL_ENTRY_OPTION for o in options)
        allowed = {str(o.id) for o in options}
        kept = {js_string_of(v) for v in coerce_choice_list(stored_value)}
        items = coerce_choice_list(value)
        for item in items:
            if is_manual_value(item):
                if not has_manual and item not in kept:
                    return "Selected option is no longer available."
                if attrs.get("mandatory") and js_trim(get_manual_text(item)) == "":
                    return "Type your manual answer."
                continue
            if js_string_of(item) not in allowed and js_string_of(item) not in kept:
                return "Selected option is no longer available."
        # The list is stored as one string (Python's `str(list)`) in a
        # varchar(255); past that the server rejects the whole save.
        stored = stored_choice_list_length(items) if isinstance(value, list) else len(js_string_of(value))
        if not stored_unchanged and stored > TEXT_ANSWER_MAX_LEN:
            if any(is_manual_value(item) for item in items):
                return "Too long to save — shorten the typed answer or pick fewer options."
            return "Too many options selected to save — pick fewer."
        return None
    if q_type == QType.LOCATION:
        if not value:
            return None
        lat = value.get("lat") if isinstance(value, Mapping) else None
        lng = value.get("lng") if isinstance(value, Mapping) else None
        if not (is_number(lat) and math.isfinite(lat)) or lat < -90 or lat > 90:
            return "Latitude must be between -90 and 90."
        if not (is_number(lng) and math.isfinite(lng)) or lng < -180 or lng > 180:
            return "Longitude must be between -180 and 180."
        return None
    return None


def visible_options_for(question: Question, evaluation: EvaluationResult, tk: TabKey) -> List[Option]:
    hidden = evaluation.hidden_option_ids_by_tab.get(tk, {}).get(question.id)
    if not hidden:
        return list(question.opt_list)
    return [o for o in question.opt_list if o.id not in hidden]


def flatten_for_calc(
    answers: Answers,
    form: Form,
    own_page_id: int,
    own_page_key: int,
    hidden_ques_ids_by_tab: Optional[Mapping[TabKey, Set[int]]] = None,
) -> Dict[int, Any]:
    """One tab's flat view for computed defaults: same-page refs read the
    active entry, other pages entry 1; hidden questions are left out."""
    out: Dict[int, Any] = {}
    for page in form.page_list:
        k = own_page_key if page.id == own_page_id else 1
        hidden = hidden_ques_ids_by_tab.get(tab_key(page.id, k), ()) if hidden_ques_ids_by_tab else ()
        for question in page.ques_list:
            if question.id in hidden:
                continue
            by_key = answers.get(question.id)
            if by_key is not None and k in by_key:
                out[question.id] = by_key[k]
    return out
