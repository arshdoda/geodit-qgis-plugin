"""Computed default values and schema-built unique ids.

Port of geodit-ui ``utils/evaluateDefaultValue.ts`` (MANUAL, CALCULATE and
SHAPEFILE defaults — PREVIOUS lives in a browser cache the embedded answer
sheet never reads), ``utils/schema-parts.ts``, ``utils/computeUniqueId.ts`` and
the codecs of ``utils/manual-default-store.ts``.

CALCULATE expressions are parsed by the ported tokenizer and recursive-descent
parser below — never ``eval`` — so an authored default can only ever combine
answers with ``+ − × ÷`` and parentheses.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Set

from .answers import (
    choice_labels_for,
    coerce_numeric,
    display_answer_string,
    find_manual_option,
    is_absolute_media_url,
    js_string_of,
    to_manual_answer,
)
from .jsnum import as_int_if_integral, is_integer, is_number, js_number, js_round, js_str, js_trim
from .model import MEDIA_QTYPES, DefaultType, Form, IdentityType, QType, Question
from .rules import evaluate_condition
from .timezone import normalize_wall_clock

NUMERIC_QTYPES = frozenset({QType.NUMBER, QType.DECIMAL, QType.COUNTER, QType.RATING, QType.MEASUREMENT})
TEXT_QTYPES = frozenset({QType.TEXT, QType.ID, QType.PARAGRAPH, QType.IDENTITY, QType.EMAIL, QType.PHONE})

FlatAnswers = Mapping[int, Any]  # one tab's `{ques_id: value}` view


def _default_type(question: Question) -> float:
    return (
        js_number(question.attr("default_type"))
        if not is_number(question.attr("default_type"))
        else float(question.attr("default_type"))
    )


def _default_value(question: Question) -> str:
    raw = question.attr("default_value")
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else js_str(raw) if is_number(raw) else str(raw)


# ------------------------------------------------------------------ codecs
def decode_multi_select(stored: str) -> List[str]:
    if not stored:
        return []
    try:
        parsed = json.loads(stored)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [js_string_of(v) for v in parsed]


_LOCATION = re.compile(r"^\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*$")


def decode_location(stored: str) -> Optional[Dict[str, float]]:
    """A MANUAL location default, ``"lat,lng"``."""
    if not stored:
        return None
    match = _LOCATION.match(stored)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return None
    return {"lat": as_int_if_integral(lat), "lng": as_int_if_integral(lng)}


_SPACES = re.compile(r"\s+")


def normalize_identity_for_storage(raw: str, identity_type: Any = None) -> str:
    """``getIdentityPattern(type).transform`` — strip spaces / upper-case."""
    kind = identity_type if is_number(identity_type) else js_number(identity_type)
    if kind in (IdentityType.AADHAAR_CARD,):
        return _SPACES.sub("", raw)
    if kind in (IdentityType.PAN_CARD, IdentityType.VOTER_ID, IdentityType.PASSPORT):
        return _SPACES.sub("", raw.upper())
    if kind == IdentityType.DRIVING_LICENSE:
        return raw.upper()
    return raw


# ------------------------------------------------------------------ schema parts
@dataclass(frozen=True)
class SchemaPart:
    kind: str  # "question" | "separator"
    id: int = 0
    char: str = ""


_SCHEMA_TOKEN = re.compile(r"\{([0-9]+)\}|([_\-/])")


def parse_schema_parts(schema: str) -> List[SchemaPart]:
    """``{N}`` question refs interleaved with ``_ - /`` separators; anything else
    (a legacy ``+``) is dropped."""
    if not schema:
        return []
    out: List[SchemaPart] = []
    for match in _SCHEMA_TOKEN.finditer(schema):
        if match.group(1):
            ident = int(match.group(1))
            if ident > 0:
                out.append(SchemaPart("question", id=ident))
        elif match.group(2):
            out.append(SchemaPart("separator", char=match.group(2)))
    return out


def parse_schema_tokens(schema: str) -> List[int]:
    return [p.id for p in parse_schema_parts(schema) if p.kind == "question"]


# ------------------------------------------------------------------ unique ids
def unique_value_to_string(value: Any, padding: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value  # legacy digit string, already padded
    if is_number(value) and math.isfinite(value):
        base = js_str(value)
        if is_number(padding) and padding > len(base):
            return base.rjust(int(padding), "0")
        return base
    return ""


def unique_value_own(value: Any) -> Any:
    """The numeric counter of a unique-id slot, or ``""``."""
    if value is None or value == "":
        return ""
    if not (is_number(value) or isinstance(value, str)):
        return ""
    number = js_number(value)
    return as_int_if_integral(number) if math.isfinite(number) else ""


def _resolve_schema(schema: str, answers: FlatAnswers, labels) -> str:
    return "".join(
        display_answer_string(p.id, answers.get(p.id), labels) if p.kind == "question" else p.char
        for p in parse_schema_parts(schema)
    )


def _attr_text(attributes: Mapping[str, Any], key: str) -> str:
    value = attributes.get(key)
    return value if isinstance(value, str) else ""


def compute_unique_id(
    attributes: Mapping[str, Any],
    answers: FlatAnswers,
    form: Optional[Form] = None,
    stored_counter: Optional[float] = None,
) -> str:
    """``prefix + counter + suffix``. ``stored_counter`` is the counter an
    EXISTING response owns — its id renders around that, not around the form's
    configured base for the next response. An empty ref still emits its
    separators (unlike CALCULATE text)."""
    labels = choice_labels_for(form)
    prefix = _resolve_schema(_attr_text(attributes, "unique_prefix"), answers, labels)
    counter = stored_counter if stored_counter is not None else attributes.get("unique_value")
    value = js_trim(unique_value_to_string(counter, attributes.get("padding")))
    suffix = _resolve_schema(_attr_text(attributes, "unique_suffix"), answers, labels)
    return "".join(s for s in (prefix, value, suffix) if s != "")


def is_schema_unique_id(attributes: Mapping[str, Any]) -> bool:
    return bool(
        js_trim(_attr_text(attributes, "unique_prefix"))
        or js_trim(unique_value_to_string(attributes.get("unique_value"), attributes.get("padding")))
        or js_trim(_attr_text(attributes, "unique_suffix"))
    )


# ------------------------------------------------------------------ MANUAL / CALCULATE
def compute_default(question: Question, answers: FlatAnswers, form: Form) -> Any:
    """The MANUAL or CALCULATE default of ``question`` given one tab's answers;
    ``None`` when there is none (the web's ``undefined``)."""
    raw = _default_value(question)
    if not js_trim(raw):
        return None
    default_type = _default_type(question)
    if default_type == DefaultType.MANUAL:
        return _coerce_manual(question, raw)
    if default_type == DefaultType.CALCULATE:
        return _compute_calculated(question, raw, answers, form)
    return None


def _coerce_manual(question: Question, raw: str) -> Any:
    if question.q_type in NUMERIC_QTYPES:
        number = js_number(raw)
        return as_int_if_integral(number) if math.isfinite(number) else None
    if question.q_type == QType.DATETIME:
        return normalize_wall_clock(raw)
    if question.q_type == QType.LOCATION:
        return decode_location(raw)
    if question.q_type == QType.DROPDOWN:
        if find_manual_option(question.opt_list):
            manual = to_manual_answer(raw)
            if manual:
                return manual
        option = next((o for o in question.opt_list if o.value == raw), None)
        return str(option.id) if option else None
    if question.q_type == QType.MULTIPLE_CHOICE:
        has_manual = find_manual_option(question.opt_list) is not None
        id_by_value = {}
        for option in question.opt_list:
            id_by_value[option.value] = str(option.id)
        decoded = decode_multi_select(raw)
        values = decoded if decoded else [raw]
        picked = []
        for value in values:
            resolved = (to_manual_answer(value) if has_manual else None) or id_by_value.get(value)
            if resolved:
                picked.append(resolved)
        return picked or None
    return raw


def _compute_calculated(question: Question, raw: str, answers: FlatAnswers, form: Form) -> Any:
    trimmed = js_trim(raw)
    if trimmed.startswith("["):
        return _evaluate_conditional(question, trimmed, answers, form)
    return _evaluate_basic(question, trimmed, answers, form)


def _answer_key(ques_id: Any) -> Any:
    """``answers[ques_id]`` in JavaScript reads an object key: ``5`` and ``"5"``
    are the same key, ``"05"`` is not."""
    if is_number(ques_id) and float(ques_id).is_integer():
        return int(ques_id)
    if isinstance(ques_id, str) and ques_id.isdigit() and str(int(ques_id)) == ques_id:
        return int(ques_id)
    return ques_id


def _eval_sub_condition(sub: Mapping[str, Any], answers: FlatAnswers, ques_by_id: Mapping[int, Question]) -> bool:
    ques_id = sub.get("ques_id")
    operator = sub.get("operator")
    if ques_id is None or operator is None:
        return False
    # A Map keyed by number: only a numeric id finds its question.
    question = ques_by_id.get(int(ques_id)) if is_number(ques_id) and float(ques_id).is_integer() else None
    synthetic: Dict[str, Any] = {"operator": operator}
    if sub.get("value") is not None:
        synthetic["cond_val_s"] = sub["value"]
    return evaluate_condition(question, synthetic, answers.get(_answer_key(ques_id)))


def _evaluate_conditional(question: Question, serialized: str, answers: FlatAnswers, form: Form) -> Any:
    try:
        rows = json.loads(serialized)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    ques_by_id = form.question_by_id()
    else_row = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("condition") if row.get("condition") is not None else "").lower()
        if kind == "else":
            else_row = row
            continue
        if kind not in ("if", "elseif"):
            continue
        conditions = row.get("conditions")
        if isinstance(conditions, list) and conditions:
            subs = [c if isinstance(c, Mapping) else {} for c in conditions]
        else:
            subs = [{"ques_id": row.get("ques_id"), "operator": row.get("operator"), "value": row.get("value")}]
        logic = str(row.get("logic_operator") if row.get("logic_operator") is not None else "AND").upper()
        if logic == "OR":
            matched = any(_eval_sub_condition(s, answers, ques_by_id) for s in subs)
        else:
            matched = all(_eval_sub_condition(s, answers, ques_by_id) for s in subs)
        if matched:
            return _evaluate_basic(question, _then_text(row), answers, form)
    if else_row is not None:
        return _evaluate_basic(question, _then_text(else_row), answers, form)
    return None


def _then_text(row: Mapping[str, Any]) -> str:
    then = row.get("then")
    if then is None:
        return ""
    return then if isinstance(then, str) else js_str(then) if is_number(then) else str(then)


def _evaluate_basic(question: Question, expression: str, answers: FlatAnswers, form: Form) -> Any:
    if not js_trim(expression):
        return None
    if question.q_type in TEXT_QTYPES:
        return _evaluate_separator_expression(expression, answers, form)
    if question.q_type in NUMERIC_QTYPES:
        tokens = _tokenize(expression)
        if tokens is None:
            return None
        result = _evaluate_numeric(tokens, answers)
        if result is None or not math.isfinite(result):
            return None
        if question.q_type in (QType.NUMBER, QType.COUNTER):
            return js_round(result)
        return as_int_if_integral(result)
    return None


# Tokens: ("num", value) | ("ref", id) | ("op", "+-*/") | ("(",) | (")",)
_OPS = {"+": "+", "−": "-", "-": "-", "×": "*", "*": "*", "÷": "/", "/": "/"}
_NUMBER_CHARS = set("0123456789.")


def _tokenize(text: str) -> Optional[List[tuple]]:
    tokens: List[tuple] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in (" ", "\t", "\n"):
            i += 1
            continue
        if ch in _OPS:
            tokens.append(("op", _OPS[ch]))
            i += 1
            continue
        if ch in "()":
            tokens.append((ch,))
            i += 1
            continue
        if ch == "{":
            close = text.find("}", i + 1)
            if close == -1:
                return None
            ident = js_number(text[i + 1 : close])
            if not is_integer(ident):
                return None
            tokens.append(("ref", int(ident)))
            i = close + 1
            continue
        if ch in _NUMBER_CHARS:
            j = i
            while j < len(text) and text[j] in _NUMBER_CHARS:
                j += 1
            value = js_number(text[i:j])
            if not math.isfinite(value):
                return None
            tokens.append(("num", value))
            i = j
            continue
        return None
    return tokens


class _Parser:
    def __init__(self, tokens: List[tuple], answers: FlatAnswers) -> None:
        self.tokens = tokens
        self.answers = answers
        self.pos = 0

    def _peek(self) -> Optional[tuple]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def factor(self) -> Optional[float]:
        tok = self._peek()
        if tok is None:
            return None
        if tok[0] == "num":
            self.pos += 1
            return tok[1]
        if tok[0] == "ref":
            self.pos += 1
            value = coerce_numeric(self.answers.get(tok[1]))
            return None if value is None else js_number(value)
        if tok[0] == "(":
            self.pos += 1
            value = self.expr()
            if value is None:
                return None
            nxt = self._peek()
            if nxt is None or nxt[0] != ")":
                return None
            self.pos += 1
            return value
        if tok[0] == "op" and tok[1] in "+-":
            self.pos += 1
            nxt_value = self.factor()
            if nxt_value is None:
                return None
            return -nxt_value if tok[1] == "-" else nxt_value
        return None

    def term(self) -> Optional[float]:
        left = self.factor()
        if left is None:
            return None
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            if tok[0] != "op" or tok[1] not in "*/":
                break
            self.pos += 1
            right = self.factor()
            if right is None:
                return None
            if tok[1] == "*":
                left = left * right
            else:
                if right == 0:
                    return None  # division by zero: the default stays unresolved
                left = left / right
        return left

    def expr(self) -> Optional[float]:
        left = self.term()
        if left is None:
            return None
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            if tok[0] != "op" or tok[1] not in "+-":
                break
            self.pos += 1
            right = self.term()
            if right is None:
                return None
            left = left + right if tok[1] == "+" else left - right
        return left


def _evaluate_numeric(tokens: List[tuple], answers: FlatAnswers) -> Optional[float]:
    parser = _Parser(tokens, answers)
    try:
        result = parser.expr()
    except OverflowError:
        return None
    if parser.pos != len(tokens):
        return None
    return result


class _AllOnes(dict):
    def get(self, key, default=None):  # noqa: D401 - dict protocol
        return 1


def is_complete_numeric_expression(expression: str) -> bool:
    """Can this numeric expression ever produce a value? (every ref reads 1)"""
    if not js_trim(expression):
        return True
    tokens = _tokenize(expression)
    if tokens is None:
        return False
    return _evaluate_numeric(tokens, _AllOnes()) is not None


def _evaluate_separator_expression(expression: str, answers: FlatAnswers, form: Form) -> Optional[str]:
    """Text defaults are templates: refs interleaved with ``_ - /``. An empty
    ref is skipped (its separators stay); nothing resolves → no default."""
    labels = choice_labels_for(form)
    out = ""
    got_value = False
    for part in parse_schema_parts(expression):
        if part.kind == "question":
            text = display_answer_string(part.id, answers.get(part.id), labels)
            if not text:
                continue
            out += text
            got_value = True
        else:
            out += part.char
    return out if got_value else None


def collect_calc_ref_ques_ids(question: Question) -> Set[Any]:
    """Question ids a CALCULATE default reads — condition ids plus every ``{N}``
    ref. Looser than evaluation on purpose (it can only lift a pause early)."""
    refs: Set[Any] = set()
    if _default_type(question) != DefaultType.CALCULATE:
        return refs
    trimmed = js_trim(_default_value(question))
    if not trimmed:
        return refs
    if trimmed.startswith("["):
        try:
            rows = json.loads(trimmed)
        except ValueError:
            rows = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                conditions = row.get("conditions")
                if isinstance(conditions, list):
                    for sub in conditions:
                        if isinstance(sub, Mapping) and sub.get("ques_id") is not None:
                            refs.add(sub["ques_id"])
                if row.get("ques_id") is not None:
                    refs.add(row["ques_id"])
                refs.update(parse_schema_tokens(_then_text(row)))
            return refs
    refs.update(parse_schema_tokens(trimmed))
    return refs


# ------------------------------------------------------------------ SHAPEFILE
def form_has_shapefile_defaults(form: Form) -> bool:
    return any(_default_type(q) == DefaultType.SHAPEFILE and js_trim(_default_value(q)) != "" for q in form.questions())


def resolve_shapefile_default(question: Question, feature_attributes: Mapping[str, Any]) -> Any:
    """The candidate answer a SHAPEFILE default takes from the feature's
    attribute (``default_value`` names it). A CANDIDATE only: the caller seeds
    it only if ``validate_value`` accepts it."""
    if _default_type(question) != DefaultType.SHAPEFILE:
        return None
    attr_name = js_trim(_default_value(question))
    if not attr_name:
        return None
    raw = feature_attributes.get(attr_name)
    if raw is None:
        return None
    raw_str = js_trim(raw if isinstance(raw, str) else js_str(raw) if is_number(raw) else str(raw))
    if not raw_str:
        return None
    if question.q_type in MEDIA_QTYPES:
        # One attribute cell can only carry a link; anything else seeds nothing.
        return raw_str if is_absolute_media_url(raw_str) else None
    if question.q_type in NUMERIC_QTYPES:
        number = float(raw) if is_number(raw) else js_number(raw_str)
        return as_int_if_integral(number) if math.isfinite(number) else None
    if question.q_type == QType.DROPDOWN:
        option = next((o for o in question.opt_list if o.value == raw_str), None)
        return str(option.id) if option else None
    if question.q_type == QType.IDENTITY:
        return normalize_identity_for_storage(raw_str, question.attr("identity_type"))
    if question.q_type == QType.PHONE:
        return re.sub(r"[^0-9]", "", raw_str) or None
    if question.q_type == QType.DATETIME:
        return normalize_wall_clock(raw_str)
    if question.q_type in (QType.TEXT, QType.PARAGRAPH, QType.EMAIL, QType.ID):
        return raw_str
    return None
