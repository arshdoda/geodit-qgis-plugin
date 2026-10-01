"""The cross-record half of the UNIQUE rule: the pre-save probe.

Port of geodit-ui ``runtime/uniqueCheck.ts`` and the batching of
``findAnsUniqueViolations`` (``components/data/services/api.ts``).
``POST /data/{proj}/ans-unique-constraint`` answers whether ONE stored response
already carries every item of a combination; the writes never enforce it, so
this advisory probe is the only gate. Invariants:

* one request per UNIQUE rule — never merge two rules into one body;
* a rule's repeating-page entries ride in that request as separate ``group``s;
* never split a group across requests, and never send a partial tuple;
* both server caps bind (50 items, 20 groups per request); an oversized rule
  is split into batches of WHOLE groups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .answers import Answers, TabKey, coerce_numeric, get_answer, tab_key
from .defaults import is_schema_unique_id
from .jsnum import js_str
from .model import QType, Question
from .timezone import normalize_wall_clock
from .validation import is_empty_for_question

UNIQUE_CHECK_MAX_ITEMS = 50
UNIQUE_CHECK_MAX_GROUPS = 20
UNIQUE_CHECK_UNAVAILABLE = "Couldn't verify that these answers are unique. Check your connection and try again."
UNIQUE_VIOLATION = re.compile(r"unique constraint violated", re.IGNORECASE)

_INT_TYPES = frozenset({QType.ID, QType.NUMBER, QType.RATING, QType.COUNTER, QType.PHONE})
_DEC_TYPES = frozenset({QType.DECIMAL, QType.MEASUREMENT})
_MEDIA_TYPES = frozenset({QType.IMAGE, QType.DOCUMENT, QType.AUDIO, QType.VIDEO, QType.SIGNATURE})

_SKIP = object()


@dataclass
class UniqueCheckItem:
    ques_id: int
    ques_type: int
    page_key: int
    value: Any

    def wire(self, group: int) -> Dict[str, Any]:
        return {
            "ques_id": self.ques_id,
            "ques_type": self.ques_type,
            "page_key": self.page_key,
            "value": self.value,
            "group": group,
        }


@dataclass
class UniqueCheckGroup:
    rule_id: int
    page_key: int
    items: List[UniqueCheckItem] = field(default_factory=list)


def to_unique_check_value(question: Question, value: Any) -> Any:
    """The value to probe for one slot, shaped as the write path stores it — or
    ``_SKIP`` (empty, LOCATION, a schema-unique ID, an unparseable number)."""
    if is_empty_for_question(question, value):
        return _SKIP
    if question.q_type == QType.LOCATION:
        return _SKIP
    if question.q_type == QType.ID and is_schema_unique_id(question.attributes):
        return _SKIP
    if question.q_type in _INT_TYPES:
        number = coerce_numeric(value)
        if number is None:
            return _SKIP
        return int(number) if float(number).is_integer() else number
    if question.q_type in _DEC_TYPES:
        number = coerce_numeric(value)
        return _SKIP if number is None else js_str(number)
    if question.q_type in _MEDIA_TYPES and isinstance(value, Mapping) and "url" in value:
        key = value.get("key") or value.get("url")
        return key if key else _SKIP
    if question.q_type == QType.DATETIME:
        normalized = normalize_wall_clock(value)
        return normalized if normalized is not None else _SKIP
    return value


def build_unique_check_groups(
    *,
    answers: Answers,
    ques_by_id: Mapping[int, Question],
    target_groups: Sequence[Tuple[int, List[int]]],
    page_keys_by_page_id: Mapping[int, List[int]],
    hidden_ques_ids_by_tab: Mapping[TabKey, Set[int]],
) -> List[UniqueCheckGroup]:
    """One group per UNIQUE rule per live repeating-page entry: the rule's full
    target set at its CURRENT values (the host sends ``exclude_ans_id`` on
    update, so a response's own values can only match OTHER responses). A group
    is emitted only when every target produced a value."""
    out: List[UniqueCheckGroup] = []
    for rule_id, target_ids in target_groups:
        targets = [ques_by_id[i] for i in target_ids if i in ques_by_id]
        if not targets:
            continue
        keys_by_target = [
            list(page_keys_by_page_id[q.page_id]) if q.page_id in page_keys_by_page_id else [1] for q in targets
        ]
        iteration_keys = sorted({k for keys in keys_by_target for k in keys})
        for iteration_key in iteration_keys:
            items: List[UniqueCheckItem] = []
            complete = True
            for question, keys in zip(targets, keys_by_target):
                page_key = iteration_key if iteration_key in keys else keys[0]
                if question.id in hidden_ques_ids_by_tab.get(tab_key(question.page_id, page_key), ()):
                    complete = False
                    break
                probe = to_unique_check_value(question, get_answer(answers, question.id, page_key))
                if probe is _SKIP:
                    complete = False
                    break
                items.append(UniqueCheckItem(question.id, question.q_type, page_key, probe))
            if complete and items:
                out.append(UniqueCheckGroup(rule_id, iteration_key, items))
    return out


def batch_unique_groups(groups: Sequence[UniqueCheckGroup]) -> List[List[UniqueCheckGroup]]:
    """Requests to send: one per rule, a rule over either cap split into
    batches of whole groups."""
    by_rule: Dict[int, List[UniqueCheckGroup]] = {}
    for group in groups:
        by_rule.setdefault(group.rule_id, []).append(group)
    batches: List[List[UniqueCheckGroup]] = []
    for rule_groups in by_rule.values():
        batch: List[UniqueCheckGroup] = []
        size = 0
        for group in rule_groups:
            over_items = size + len(group.items) > UNIQUE_CHECK_MAX_ITEMS
            over_groups = len(batch) >= UNIQUE_CHECK_MAX_GROUPS
            if batch and (over_items or over_groups):
                batches.append(batch)
                batch, size = [], 0
            batch.append(group)
            size += len(group.items)
        if batch:
            batches.append(batch)
    return batches


def batch_body(batch: Sequence[UniqueCheckGroup]) -> List[Dict[str, Any]]:
    """The request body: ``group`` indexes within THIS request."""
    return [item.wire(i) for i, group in enumerate(batch) for item in group.items]


def flagged_items(batch: Sequence[UniqueCheckGroup], violated_groups: Optional[Sequence[int]]) -> List[UniqueCheckItem]:
    """The items to flag for a batch the server called a violation: every item
    of the named groups, or of the whole batch when it named none (fail-safe:
    over-highlight, never under-block)."""
    if violated_groups is None:
        chosen = list(batch)
    else:
        chosen = [g for i, g in enumerate(batch) if i in set(violated_groups)] or list(batch)
    return [item for group in chosen for item in group.items]


def parse_violated_groups(body: Any) -> Optional[List[int]]:
    raw = body.get("groups") if isinstance(body, Mapping) else None
    if not isinstance(raw, list):
        return None
    groups = []
    for value in raw:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if number.is_integer():
            groups.append(int(number))
    return groups or None
