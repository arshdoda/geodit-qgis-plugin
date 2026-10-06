"""Answer values, manual entry and choice labels.

Port of geodit-ui ``utils/answers.ts``, ``runtime/manualEntry.ts``,
``utils/choice-display.ts`` and the media-answer helpers of ``runtime/types.ts``.

An answer value is one of: ``str``, a number, a ``list`` of option-id strings
(MULTIPLE_CHOICE), ``{"lat", "lng"}`` (LOCATION), ``{"url", "key", "name"}``
(a media file uploaded here — a stored one reads back as the bare key string),
or ``None``. Answers are ``{ques_id: {page_key: value}}``: repeating (FOR) page
entries share a question id under different page keys. A slot that is PRESENT
holding ``None`` is not the same as an absent slot — the web's
``!== undefined`` checks are ported as ``in`` checks.
"""

from __future__ import annotations

import math
import re
import weakref
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import unquote

from .jsnum import as_float, is_number, js_number, js_str, js_trim
from .model import CHOICE_QTYPES, MANUAL_ENTRY_OPTION, TEXT_ANSWER_MAX_LEN, Form, Option, Question

Answers = Dict[int, Dict[int, Any]]
TabKey = Tuple[int, int]  # (page_id, page_key) — the web's `"pageId,pageKey"`
SlotKey = Tuple[int, int]  # (ques_id, page_key) — the web's `errorKey`

MANUAL_ENTRY_LABEL = "Enter Manually"
MANUAL_VALUE_PREFIX = f"{MANUAL_ENTRY_OPTION}:"
# The longest manual text a DROPDOWN answer can store: api-v2 keeps it in a
# varchar(255) and rejects the whole save past that, and the prefix counts.
# Android caps typing at the same 246.
MANUAL_TEXT_MAX_LEN = TEXT_ANSWER_MAX_LEN - len(MANUAL_VALUE_PREFIX)

_ABSOLUTE_URL = re.compile(r"^https?://", re.IGNORECASE)


def tab_key(page_id: int, page_key: int) -> TabKey:
    return (int(page_id), int(page_key))


def error_key(ques_id: int, page_key: int) -> SlotKey:
    return (int(ques_id), int(page_key))


# ------------------------------------------------------------------ values
def is_absolute_media_url(value: str) -> bool:
    return bool(_ABSOLUTE_URL.match(js_trim(value)))


def is_location(value: Any) -> bool:
    return isinstance(value, Mapping) and "lat" in value and "lng" in value


def is_media_object(value: Any) -> bool:
    return isinstance(value, Mapping) and "url" in value


def is_empty_answer(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return js_trim(value) == ""
    if is_number(value):
        return isinstance(value, float) and math.isnan(value)
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, Mapping):
        if "lat" in value and "lng" in value:
            return _is_nan(value.get("lat")) or _is_nan(value.get("lng"))
        if "url" in value:
            return not value.get("url")
    return False


def _is_nan(value: Any) -> bool:
    return is_number(value) and isinstance(value, float) and math.isnan(value)


def _strict_equal(a: Any, b: Any) -> bool:
    """JavaScript ``===`` for the scalars an answer list holds."""
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if is_number(a) and is_number(b):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    return a is b


def same_answer_value(a: Any, b: Any) -> bool:
    """Structural equality narrow to the answer shapes — never ``coerce_string``
    equality, which would make "cleared" equal "never answered"."""
    if a is b:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)):
            return False
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if "lat" in a and "lng" in a and "lat" in b and "lng" in b:
            return _strict_equal(a.get("lat"), b.get("lat")) and _strict_equal(a.get("lng"), b.get("lng"))
        # Media compares on `key` alone: the durable object identity.
        if "url" in a and "url" in b:
            return _strict_equal(a.get("key"), b.get("key"))
        return False
    return _strict_equal(a, b)


def coerce_numeric(value: Any) -> Optional[float]:
    if value is None:
        return None
    if is_number(value):
        return value if math.isfinite(as_float(value)) else None
    if isinstance(value, str):
        text = js_trim(value)
        if text == "":
            return None
        number = js_number(text)
        return number if math.isfinite(number) else None
    return None


def _js_join_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(_js_join_part(v) for v in value)
    return js_string_of(value)


def js_string_of(value: Any) -> str:
    """``String(value)`` for any JSON value."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool) or is_number(value):
        return js_str(value)
    if isinstance(value, list):
        return ",".join(_js_join_part(v) for v in value)
    return "[object Object]"


def coerce_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if is_number(value):
        return js_str(value)
    if isinstance(value, list):
        return ",".join(_js_join_part(v) for v in value)
    if isinstance(value, Mapping):
        # Object answers need a STABLE, DISTINCT string: returning "" for all of
        # them would make every two locations / files a UNIQUE duplicate.
        if "lat" in value and "lng" in value:
            return f"{js_string_of(value.get('lat'))},{js_string_of(value.get('lng'))}"
        if "url" in value:
            return value.get("key") or value.get("url") or ""
    return ""


def coerce_choice_list(value: Any) -> List[str]:
    """DISPLAY / VALIDATION ONLY — a MULTIPLE_CHOICE answer as a list (a legacy
    bare string reads as one pick). Never write the coerced list back."""
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, str) and value else []


# ------------------------------------------------------------------ answers map
def get_answer(answers: Mapping[int, Mapping[int, Any]], ques_id: int, page_key: int) -> Any:
    by_key = answers.get(ques_id)
    if not by_key:
        return None
    return by_key.get(page_key)


def has_answer(answers: Mapping[int, Mapping[int, Any]], ques_id: int, page_key: int) -> bool:
    """The slot is present — even holding ``None`` (the web's ``!== undefined``)."""
    by_key = answers.get(ques_id)
    return by_key is not None and page_key in by_key


def set_answer_at(answers: Mapping[int, Mapping[int, Any]], ques_id: int, page_key: int, value: Any) -> Answers:
    out = dict(answers)
    out[ques_id] = {**(answers.get(ques_id) or {}), page_key: value}
    return out  # type: ignore[return-value]


def remap_page_keys(
    answers: Mapping[int, Mapping[int, Any]], ques_ids_on_page: Set[int], remap: Mapping[int, Optional[int]]
) -> Answers:
    """Apply an ``old key → new key`` remap to every answer of the given
    questions; a ``None`` new key drops the entry. Used when a repeating-page
    entry is removed and the rest are resequenced to 1..N."""
    if not ques_ids_on_page or not remap:
        return answers  # type: ignore[return-value]
    mutated = False
    out: Answers = dict(answers)  # type: ignore[arg-type]
    for ques_id in ques_ids_on_page:
        by_key = answers.get(ques_id)
        if by_key is None:
            continue
        rebuilt: Dict[int, Any] = {}
        for old_key, value in by_key.items():
            if old_key not in remap:
                rebuilt[old_key] = value
                continue
            mapped = remap[old_key]
            mutated = True
            if mapped is None:
                continue
            rebuilt[mapped] = value
        out[ques_id] = rebuilt
    return out if mutated else answers  # type: ignore[return-value]


def remap_key_list(keys: Iterable[int], remap: Mapping[int, Optional[int]]) -> List[int]:
    """Apply the same removal remap to a list of page keys (``remapKeyList``):
    the removed key drops out, the keys above it shift down, and a key the remap
    doesn't name passes through."""
    out: List[int] = []
    for key in keys:
        if key not in remap:
            out.append(key)
        elif remap[key] is not None:
            out.append(remap[key])  # type: ignore[arg-type]
    return out


def flatten_tab(answers: Mapping[int, Mapping[int, Any]], page_key: int) -> Dict[int, Any]:
    return {ques_id: by_key[page_key] for ques_id, by_key in answers.items() if page_key in by_key}


# ------------------------------------------------------------------ manual entry
def find_manual_option(options: Iterable[Option]) -> Optional[Option]:
    return next((o for o in options if o.value == MANUAL_ENTRY_OPTION), None)


def is_manual_value(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MANUAL_VALUE_PREFIX)


def get_manual_text(value: Any) -> str:
    return value[len(MANUAL_VALUE_PREFIX) :] if is_manual_value(value) else ""


def build_manual_value(text: str) -> str:
    return f"{MANUAL_VALUE_PREFIX}{text}"


def get_option_label(option: Option) -> str:
    return MANUAL_ENTRY_LABEL if option.value == MANUAL_ENTRY_OPTION else option.value


def is_manual_selection(value: Any) -> bool:
    return value == MANUAL_ENTRY_OPTION or is_manual_value(value)


def to_manual_answer(value: str) -> Optional[str]:
    """A stored choice-default token in the runtime's shape: ``_manual_:<text>``
    verbatim, a legacy bare ``_manual_`` promoted to ``_manual_:``."""
    if is_manual_value(value):
        return value
    if value == MANUAL_ENTRY_OPTION:
        return build_manual_value("")
    return None


def canonical_choice_order(values: Sequence[Any], options: Sequence[Option]) -> List[Any]:
    """A MULTIPLE_CHOICE selection in the one order every platform writes: each
    option once, by the options' ``position`` and then their id (``position``
    isn't unique — api-v2 defaults it to 0 — so the id settles ties, as
    geodit-celery's import does), then ids no option matches (in their own
    order), then the manual entry. api-v2 stores and compares the list as one
    string, so in click order the same picks would be two different answers.
    Pass the question's FULL option list, so a pick that rules hide keeps its
    rank."""
    rank = {str(o.id): i for i, o in enumerate(sorted(options, key=lambda o: (o.position, o.id)))}
    picks = list(dict.fromkeys(v for v in values if not is_manual_selection(v)))
    return [
        *sorted((v for v in picks if v in rank), key=rank.__getitem__),
        *(v for v in picks if v not in rank),
        *(v for v in values if is_manual_selection(v)),
    ]


def stored_choice_list_length(values: Sequence[Any]) -> int:
    """How long a MULTIPLE_CHOICE answer is once stored: api-v2 writes the list
    as Python's ``str(list)`` — ``['353', '_manual_:Pune']`` — into the same
    varchar(255), so the brackets, quotes, ``, `` separators and repr escapes
    all count, in code points. The web emulates that repr; this is the real one."""
    return len(str(list(values)))


# ------------------------------------------------------------------ choice labels
ChoiceLabels = Dict[int, Dict[str, str]]
_LABEL_CACHE: weakref.WeakKeyDictionary[Form, ChoiceLabels] = weakref.WeakKeyDictionary()


def option_labels_by_id(question: Question) -> Optional[Dict[str, str]]:
    if question.q_type not in CHOICE_QTYPES:
        return None
    return {str(o.id): get_option_label(o) for o in question.opt_list}


def choice_labels_for(form: Optional[Form]) -> ChoiceLabels:
    if form is None:
        return {}
    cached = _LABEL_CACHE.get(form)
    if cached is not None:
        return cached
    labels: ChoiceLabels = {}
    for question in form.questions():
        by_id = option_labels_by_id(question)
        if by_id is not None:
            labels[question.id] = by_id
    _LABEL_CACHE[form] = labels
    return labels


def choice_display_text(token: str, labels: Optional[Mapping[str, str]]) -> str:
    """One stored choice token → its text: a manual entry's typed text (blank
    when empty), an option's label, or the token itself when it matches none."""
    if is_manual_value(token):
        return get_manual_text(token)
    if labels is not None and token in labels:
        return labels[token]
    return token


def display_answer_string(ques_id: int, value: Any, labels: ChoiceLabels) -> str:
    """A referenced answer stringified for concatenation (CALCULATE text,
    unique-id prefix/suffix): choice answers resolve to labels."""
    by_id = labels.get(ques_id)
    if by_id is None:
        return coerce_string(value)
    if isinstance(value, list):
        return ",".join(t for t in (choice_display_text(js_string_of(v), by_id) for v in value) if t)
    if isinstance(value, str) or is_number(value):
        return choice_display_text(js_string_of(value), by_id)
    return coerce_string(value)


# ------------------------------------------------------------------ media answers
def file_answer_from_value(value: Any) -> Optional[Dict[str, str]]:
    """A media answer as ``{url, key, name}`` for display: an uploaded object as
    is, a stored bare key (or absolute URL) with a name from its last path part.
    Display-only — the stored value itself is never rewritten."""
    if isinstance(value, Mapping) and "url" in value and "key" in value:
        return {"url": value.get("url") or "", "key": value.get("key") or "", "name": value.get("name") or ""}
    if isinstance(value, str) and js_trim(value):
        path = re.split(r"[?#]", js_trim(value))[0] if is_absolute_media_url(value) else value
        leaf = re.split(r"[\\/]", path)[-1]
        try:
            decoded = unquote(leaf, errors="strict")
        except UnicodeDecodeError:
            decoded = leaf
        name = re.sub(r"^\d{10,}-", "", decoded) or "file"
        return {"url": "", "key": value, "name": name}
    return None


def direct_media_url(file: Optional[Mapping[str, str]]) -> Optional[str]:
    """An absolute ``http(s)`` value renders without the presign round trip."""
    if file and is_absolute_media_url(file.get("key") or ""):
        return js_trim(file["key"])
    return None


def collect_file_answer_keys(answers: Mapping[int, Mapping[int, Any]]) -> List[str]:
    keys: List[str] = []
    for by_key in answers.values():
        for value in by_key.values():
            if isinstance(value, Mapping) and isinstance(value.get("key"), str) and value.get("key"):
                keys.append(value["key"])
    return keys


def normalize_answers(raw: Mapping[Any, Mapping[Any, Any]]) -> Answers:
    """Int-keyed copy of an answers map (JSON round trips turn keys into strings)."""
    out: Answers = {}
    for ques_id, by_key in raw.items():
        out[int(ques_id)] = {int(k): v for k, v in by_key.items()}
    return out
