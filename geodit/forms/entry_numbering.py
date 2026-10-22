"""Each repeating entry's own generated-ID number (R6b).

Port of geodit-ui ``runtime/entryNumbering.ts``. A generated ID is an ``ID``
question with a prefix / counter / suffix schema. It numbers every entry of a
repeating page on its own (the user's decision, 2026-10-08): Room 1
``RM-0007``, Room 2 ``RM-0008``. An entry this session numbers gets
``start + ordinal``:

* the ordinal is the entry's place in the order this session numbered
  entries. It is frozen for the entry's life and moves with it when an earlier
  entry is removed, so a removal leaves a gap rather than renumbering;
* the start is one past the highest of the response's stored numbers and the
  server's ``latest-id``, or, on a create with neither, the form's configured
  Starting Value.

The number is provisional: api-v2 settles a clash at save and echoes what it
stored (``id_counters``).

The web also numbers a key a create holds without an ordinal (a restored
draft). QGIS has no drafts, and every live key of a create gets its ordinal at
open, from ``add_entry`` or through a removal, so that case isn't ported.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence

from .answers import Answers
from .defaults import is_schema_unique_id, unique_value_own, unique_value_to_string
from .jsnum import is_number
from .model import QType, Question
from .validation import INT4_MAX

# The highest counter a slot can store: api-v2 keeps it in an int4 column
# (Android's ``UniqueId.MAX_COUNTER``).
MAX_COUNTER = INT4_MAX

# Per page, each live key's place in the order this session numbered entries; 0 is the first.
EntryOrdinals = Dict[int, Dict[int, int]]


def clamp_counter(n: float) -> int:
    """``Math.min(MAX_COUNTER, Math.max(0, Math.trunc(n)))``, an infinity
    included (``math.trunc`` alone would raise)."""
    if not math.isfinite(n):
        return MAX_COUNTER if n > 0 else 0
    return min(MAX_COUNTER, max(0, math.trunc(n)))


def is_generated_id(question: Question) -> bool:
    return question.q_type == QType.ID and is_schema_unique_id(question.attributes)


def seed_entry_ordinals(
    page_ids: Iterable[int], page_keys_by_page_id: Mapping[int, Sequence[int]], creating: bool
) -> EntryOrdinals:
    """A create numbers every entry it opens with, in key order; an update
    numbers none of the entries it opens with."""
    if not creating:
        return {}
    return {
        page_id: {key: i for i, key in enumerate(sorted(page_keys_by_page_id.get(page_id, [1])))}
        for page_id in page_ids
    }


def next_entry_ordinal(ordinals: EntryOrdinals, page_id: int) -> int:
    """The ordinal the next entry added to a page takes: one past the page's highest."""
    taken = ordinals.get(page_id) or {}
    return max(taken.values()) + 1 if taken else 0


def with_entry_ordinal(ordinals: EntryOrdinals, page_id: int, key: int, ordinal: int) -> EntryOrdinals:
    return {**ordinals, page_id: {**ordinals.get(page_id, {}), key: ordinal}}


def remap_entry_ordinals(ordinals: EntryOrdinals, page_id: int, remap: Mapping[int, Optional[int]]) -> EntryOrdinals:
    """A removal's remap: ordinals move with their entries and the removed
    entry's goes. The same object back when the page has none."""
    by_key = ordinals.get(page_id)
    if by_key is None:
        return ordinals
    moved: Dict[int, int] = {}
    for key, ordinal in by_key.items():
        mapped = remap[key] if key in remap else key
        if mapped is not None:
            moved[mapped] = ordinal
    return {**ordinals, page_id: moved}


def counter_starts(
    questions: Iterable[Question], baseline: Optional[Answers], latest: Optional[Mapping[int, Any]]
) -> Dict[int, int]:
    """Per generated-ID question, the number the session's first numbered
    entry takes: one past the highest of what the response stored when it
    opened (every key, removed entries included) and the server's
    ``latest-id`` (when > 0). On a create (``baseline`` None) with neither, the
    form's ``unique_value``: the configured Starting Value, or ``latest + 1``
    where the create path patched it in. A stored response with neither gets
    no start, so its added entries get no number. ``{}`` is a stored response
    that stores nothing, not a create."""
    out: Dict[int, int] = {}
    for question in questions:
        if not is_generated_id(question):
            continue
        top: Any = None
        for value in ((baseline or {}).get(question.id) or {}).values():
            own = unique_value_own(value)
            if own != "" and (top is None or own > top):
                top = own
        hint = (latest or {}).get(question.id)
        if is_number(hint) and hint > 0 and (top is None or hint > top):
            top = hint
        if top is not None:
            out[question.id] = clamp_counter(top + 1)
        elif baseline is None:
            base = unique_value_own(question.attr("unique_value"))
            if base != "":
                out[question.id] = clamp_counter(base)
    return out


class RenumberedId(NamedTuple):
    """One slot of an ``id_counters`` echo whose stored number isn't the one sent."""

    ques_id: int
    page_key: int
    sent: Any
    stored: Any


def _slot_id(value: Any) -> Optional[int]:
    """A question id or page key read from JSON, as a number or a digit string
    (the web keys a slot by ``${ques_id},${page_key}``, so both match there)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def renumbered_ids(sent: Iterable[Mapping[str, Any]], echo: Any) -> List[RenumberedId]:
    """The generated IDs the server renumbered at save: each echoed slot
    (``ans-create`` / ``ans-update`` ``id_counters``) whose sent counter differs.
    Slots the save didn't send are skipped; a missing echo (an older server, a
    failed read) means none."""
    if not isinstance(echo, list):
        return []
    sent_by_slot = {(_slot_id(s.get("ques_id")), _slot_id(s.get("page_key"))): s.get("value") for s in sent}
    out: List[RenumberedId] = []
    for item in echo:
        if not isinstance(item, Mapping) or not is_number(item.get("value")):
            continue
        ques_id, page_key = _slot_id(item.get("ques_id")), _slot_id(item.get("page_key"))
        if ques_id is None or page_key is None:
            continue
        before = unique_value_own(sent_by_slot.get((ques_id, page_key)))
        if before == "" or before == item["value"]:
            continue
        out.append(RenumberedId(ques_id, page_key, before, item["value"]))
    return out


def describe_renumbered(renumbered: Sequence[RenumberedId], ques_by_id: Mapping[int, Question]) -> str:
    """One sentence per renumbered slot, padded as the question shows it:
    ``Room ID 0008 is now 0011: 0008 was already used.``"""
    sentences: List[str] = []
    for item in renumbered:
        question = ques_by_id.get(item.ques_id)
        label = (question.label if question is not None else "") or "ID"
        padding = question.attr("padding") if question is not None else None
        before = unique_value_to_string(item.sent, padding)
        after = unique_value_to_string(item.stored, padding)
        sentences.append(f"{label} {before} is now {after}: {before} was already used.")
    return " ".join(sentences)
