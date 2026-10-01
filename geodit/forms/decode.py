"""A stored response as the form seeds it.

Port of ``ansItemsToAnswers`` and its helpers (geodit-ui
``components/data/services/api.ts``). ``mobile/ans-data-list`` returns each
stored column, not the shape that was submitted; these normalizations bring
back the runtime shapes — and make the update diff exact, because the form
re-submits a seeded value verbatim:

* MULTIPLE_CHOICE comes back as Python ``str(list)`` (``"['353', '418']"``);
* LOCATION comes back as GeoJSON, **lng first**;
* DATETIME comes back with a ``Z`` its clock-time digits don't mean;
* a DROPDOWN / MULTIPLE_CHOICE value matching no option id is a manual answer
  some path saved without the ``_manual_:`` prefix — re-wrapped when the
  question offers "Enter Manually".
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .answers import Answers, build_manual_value, is_manual_value, js_string_of
from .jsnum import is_integer, is_number
from .model import MANUAL_ENTRY_OPTION, Form, QType
from .timezone import normalize_wall_clock

_PY_REPR_TOKEN = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_DIGITS = re.compile(r"^[0-9]+$")


def decode_multiple_choice_value(value: Any) -> Any:
    """A stored MULTIPLE_CHOICE value → the runtime's list of option ids: the
    Python repr, a JSON array, or a legacy all-numeric CSV. A bare string
    passes through (the render layer reads it as one pick)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [js_string_of(v) for v in parsed]
        items = [
            _ESCAPE.sub(r"\1", m.group(1) if m.group(1) is not None else (m.group(2) or ""))
            for m in _PY_REPR_TOKEN.finditer(text)
        ]
        if items:
            return items
        inner = text[1:-1].strip()
        return [p.strip() for p in inner.split(",") if p.strip()] if inner else []
    if "," in text and all(_DIGITS.match(p.strip()) for p in text.split(",")):
        return [p.strip() for p in text.split(",")]
    return value


def decode_location_value(value: Any) -> Any:
    """A stored LOCATION → ``{lat, lng}`` (it reads back as GeoJSON, lng first)."""
    if isinstance(value, Mapping):
        if is_number(value.get("lat")) and is_number(value.get("lng")):
            return value
        coords = value.get("coordinates")
        if value.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2:
            lng, lat = coords[0], coords[1]
            if is_number(lat) and is_number(lng):
                return {"lat": lat, "lng": lng}
    return value


def _normalize_choice_manual(value: Any, option_ids: set, has_manual: bool) -> Any:
    if not has_manual:
        return value

    def wrap(text: str) -> str:
        return text if text == "" or text in option_ids or is_manual_value(text) else build_manual_value(text)

    if isinstance(value, str):
        return wrap(value)
    if is_number(value):
        return wrap(js_string_of(value))
    if isinstance(value, list):
        return [wrap(js_string_of(v)) for v in value]
    return value


def ans_items_to_answers(items: Iterable[Mapping[str, Any]], form: Optional[Form] = None) -> Answers:
    """``ans-data-list`` rows → ``{ques_id: {page_key: value}}``, first write
    wins per slot, page key 1 unless the row carries an integer ≥ 1."""
    choice_meta: Dict[int, tuple] = {}
    location_ids = set()
    datetime_ids = set()
    for question in form.questions() if form is not None else []:
        if question.q_type == QType.LOCATION:
            location_ids.add(question.id)
            continue
        if question.q_type == QType.DATETIME:
            datetime_ids.add(question.id)
            continue
        is_multi = question.q_type == QType.MULTIPLE_CHOICE
        if not is_multi and question.q_type != QType.DROPDOWN:
            continue
        option_ids = {str(o.id) for o in question.opt_list}
        has_manual = any(o.value == MANUAL_ENTRY_OPTION for o in question.opt_list)
        choice_meta[question.id] = (is_multi, option_ids, has_manual)

    out: Answers = {}
    for item in items:
        if "value" not in item:
            continue
        try:
            ques_id = int(item["ques_id"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_key = item.get("page_key")
        page_key = int(raw_key) if is_integer(raw_key) and raw_key >= 1 else 1
        value = item["value"]
        if ques_id in location_ids:
            value = decode_location_value(value)
        if ques_id in datetime_ids:
            value = normalize_wall_clock(value) or value
        meta = choice_meta.get(ques_id)
        if meta is not None:
            is_multi, option_ids, has_manual = meta
            if is_multi:
                value = decode_multiple_choice_value(value)
            value = _normalize_choice_manual(value, option_ids, has_manual)
        by_key = out.setdefault(ques_id, {})
        if page_key not in by_key:
            by_key[page_key] = value
    return out


def ans_items(raw: Any) -> List[Mapping[str, Any]]:
    """The list rows of an ``ans-data-list`` response, whatever its envelope."""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, Mapping)]
    if isinstance(raw, Mapping) and isinstance(raw.get("results"), list):
        return [r for r in raw["results"] if isinstance(r, Mapping)]
    return []
