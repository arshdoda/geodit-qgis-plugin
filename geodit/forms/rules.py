"""The rules engine: IF / FOR / AT_LEAST / UNIQUE.

Port of geodit-ui ``utils/evaluateRules.ts`` (canonical cross-platform spec:
geodit-ui ``docs/rules-engine.md``) and ``utils/author-hidden.ts``.

* IF rules show / hide pages, questions and options through a fixed point
  (at most 20 passes): a trigger hidden on the tab being evaluated reads as
  empty, and per target the rules whose condition holds win over the fallback
  of the ones that don't (last write wins within each tier, in position order).
* FOR rules make pages repeatable (one tab per entry).
* AT_LEAST and UNIQUE are validation rules checked on save.

The author's "Visible to surveyor" (a page's ``visibility``, a question's
``attributes.visibility``) is deliberately NOT part of the engine: it is a
presentation filter the form applies (``FormSession``), so an author-hidden
question still triggers rules, still computes and is still submitted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .answers import (
    Answers,
    TabKey,
    coerce_numeric,
    coerce_string,
    get_answer,
    is_empty_answer,
    js_string_of,
    tab_key,
)
from .jsnum import js_number, js_number_of_null
from .model import Form, Page, QType, Question, Rule
from .timezone import normalize_wall_clock, wall_clock_key

RULE_TYPE_IF = 1
RULE_TYPE_FOR = 2
RULE_TYPE_AT_LEAST = 3
RULE_TYPE_UNIQUE = 4

OP_EQUALS = 1
OP_NOT_EQUALS = 2
OP_EMPTY = 3
OP_NOT_EMPTY = 4
OP_GREATER_THAN = 5
OP_GREATER_THAN_EQUALS = 6
OP_LESS_THAN = 7
OP_LESS_THAN_EQUALS = 8
OP_BETWEEN = 9

THEN_TYPE_PAGE = 1
THEN_TYPE_QUESTION = 2
THEN_TYPE_OPTION = 3

VIS_SHOW = 1
VIS_HIDE = 2

MAX_FIXED_POINT_ITERATIONS = 20


@dataclass
class EvaluationResult:
    hidden_page_ids: Set[int] = field(default_factory=set)
    hidden_ques_ids_by_tab: Dict[TabKey, Set[int]] = field(default_factory=dict)
    # tab → question id → hidden option ids
    hidden_option_ids_by_tab: Dict[TabKey, Dict[int, Set[int]]] = field(default_factory=dict)

    def question_hidden(self, page_id: int, page_key: int, ques_id: int) -> bool:
        return ques_id in self.hidden_ques_ids_by_tab.get(tab_key(page_id, page_key), ())


def _num(value: Any) -> float:
    return js_number(value)


class _Undefined:
    """A condition key that is absent — JavaScript ``undefined``, as opposed to a
    JSON ``null`` (which ``String()`` spells "null" and ``Number()`` reads as 0)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "undefined"


UNDEFINED: Any = _Undefined()


def _bound(condition: Mapping[str, Any], key: str) -> Any:
    return condition[key] if key in condition else UNDEFINED


def _keys(page_keys_by_page_id: Mapping[int, List[int]], page_id: Optional[int]) -> List[int]:
    """``pageKeysByPageId[id] ?? [1]`` — an empty list stays empty."""
    keys = page_keys_by_page_id.get(page_id) if page_id is not None else None  # type: ignore[arg-type]
    return [1] if keys is None else list(keys)


def _compare(left: float, right: float, op: int) -> bool:
    if op == OP_GREATER_THAN:
        return left > right
    if op == OP_GREATER_THAN_EQUALS:
        return left >= right
    if op == OP_LESS_THAN:
        return left < right
    if op == OP_LESS_THAN_EQUALS:
        return left <= right
    return False


def _compare_numeric(left: Any, right_raw: Any, op: int) -> bool:
    left_num = coerce_numeric(left)
    right_num = js_number_of_null(right_raw) if right_raw is not UNDEFINED else math.nan
    if left_num is None or not math.isfinite(right_num):
        return False
    return _compare(left_num, right_num, op)


def _compare_date(left: Any, right_raw: Any, op: int) -> bool:
    if not isinstance(left, str) or right_raw is UNDEFINED:
        return False
    left_ms = wall_clock_key(left)
    right_ms = wall_clock_key(right_raw)
    if left_ms is None or right_ms is None:
        return False
    return _compare(left_ms, right_ms, op)


def _choice_matches(question: Question, cond_val: str) -> Set[str]:
    """Answer ids that satisfy a choice condition: the author may have typed
    either an option id or its label."""
    matches = {cond_val}
    for option in question.opt_list:
        if str(option.id) == cond_val or option.value == cond_val:
            matches.add(str(option.id))
    return matches


def _as_strings(values: List[Any]) -> List[str]:
    return [js_string_of(v) for v in values]


def evaluate_condition(question: Optional[Question], condition: Mapping[str, Any], answer: Any) -> bool:
    raw_operator = condition.get("operator")
    if raw_operator is None:
        return False
    operator = _num(raw_operator)
    if not math.isfinite(operator):
        return False
    cond_s = _bound(condition, "cond_val_s")
    cond_e = _bound(condition, "cond_val_e")
    q_type = _num(question.q_type) if question is not None else math.nan
    is_choice = q_type in (QType.DROPDOWN, QType.MULTIPLE_CHOICE)
    is_date = q_type == QType.DATETIME

    if operator == OP_EMPTY:
        return is_empty_answer(answer)
    if operator == OP_NOT_EMPTY:
        return not is_empty_answer(answer)
    if operator in (OP_EQUALS, OP_NOT_EQUALS):
        if cond_s is UNDEFINED:
            return False
        equals = operator == OP_EQUALS
        text = js_string_of(cond_s)
        if is_choice and question is not None:
            matches = _choice_matches(question, text)
            if isinstance(answer, list):
                hit = any(v in matches for v in _as_strings(answer))
            else:
                hit = coerce_string(answer) in matches
            return hit if equals else not hit
        if is_date:
            if not isinstance(answer, str):
                return False
            left_ms = wall_clock_key(answer)
            right_ms = wall_clock_key(text)
            if left_ms is None or right_ms is None:
                return False
            return (left_ms == right_ms) if equals else (left_ms != right_ms)
        if isinstance(answer, list):
            hit = text in _as_strings(answer)
        else:
            hit = coerce_string(answer) == text
        return hit if equals else not hit
    if operator in (OP_GREATER_THAN, OP_GREATER_THAN_EQUALS, OP_LESS_THAN, OP_LESS_THAN_EQUALS):
        op = int(operator)
        return _compare_date(answer, cond_s, op) if is_date else _compare_numeric(answer, cond_s, op)
    if operator == OP_BETWEEN:
        if is_date:
            if not isinstance(answer, str) or cond_s is UNDEFINED or cond_e is UNDEFINED:
                return False
            left_ms, start_ms, end_ms = wall_clock_key(answer), wall_clock_key(cond_s), wall_clock_key(cond_e)
            if left_ms is None or start_ms is None or end_ms is None:
                return False
            return start_ms <= left_ms <= end_ms
        left_num = coerce_numeric(answer)
        start = js_number_of_null(cond_s) if cond_s is not UNDEFINED else math.nan
        end = js_number_of_null(cond_e) if cond_e is not UNDEFINED else math.nan
        if left_num is None or not math.isfinite(start) or not math.isfinite(end):
            return False
        return start <= left_num <= end
    return False


def compute_for_page_ids(rules: Iterable[Rule]) -> Set[int]:
    """Pages made repeatable by a FOR rule (a PAGE then of a type-2 rule)."""
    out: Set[int] = set()
    for rule in rules:
        if _num(rule.type) != RULE_TYPE_FOR:
            continue
        for then in rule.then_list:
            if _num(then.then_type) == THEN_TYPE_PAGE:
                out.add(int(then.then_id))
    return out


def _trigger_answer_at(
    trigger_id: int,
    page_key: int,
    answers: Answers,
    hidden_by_tab: Mapping[TabKey, Set[int]],
    trigger_page_id: Optional[int],
) -> Any:
    """A trigger hidden on the queried tab reads as empty — what lets cascading
    IF rules converge under the fixed point."""
    if trigger_page_id is not None and trigger_id in hidden_by_tab.get(tab_key(trigger_page_id, page_key), ()):
        return None
    return get_answer(answers, trigger_id, page_key)


def _flip(visibility: int) -> int:
    return VIS_SHOW if visibility == VIS_HIDE else VIS_HIDE


def evaluate_rules(
    *,
    rules: Iterable[Rule],
    answers: Answers,
    form: Form,
    for_page_ids: Set[int],
    page_keys_by_page_id: Mapping[int, List[int]],
) -> EvaluationResult:
    ques_by_id: Dict[int, Question] = {}
    page_id_by_ques: Dict[int, int] = {}
    ques_of_option: Dict[int, int] = {}
    ques_ids_by_page: Dict[int, List[int]] = {}
    for page in form.page_list:
        ids: List[int] = []
        for question in page.ques_list:
            ques_by_id[question.id] = question
            page_id_by_ques[question.id] = page.id
            ids.append(question.id)
            for option in question.opt_list:
                ques_of_option[option.id] = question.id
        ques_ids_by_page[page.id] = ids

    if_rules = sorted(
        (
            r
            for r in rules
            if _num(r.type) == RULE_TYPE_IF
            and r.ques_id is not None
            and r.condition is not None
            and r.ques_id in ques_by_id
        ),
        key=lambda r: r.position,
    )

    prev_pages: Set[int] = set()
    prev_ques: Dict[TabKey, Set[int]] = {}
    prev_options: Dict[TabKey, Dict[int, Set[int]]] = {}

    for _ in range(MAX_FIXED_POINT_ITERATIONS):
        # Two tiers per target: a rule ASSERTS a visibility only while its
        # condition is true; a false condition contributes a FALLBACK used only
        # when no rule on that target fires.
        page_asserted: Dict[int, int] = {}
        page_fallback: Dict[int, int] = {}
        tab_asserted: Dict[Tuple[int, int, int], int] = {}
        tab_fallback: Dict[Tuple[int, int, int], int] = {}

        for rule in if_rules:
            trigger_id = int(rule.ques_id)  # type: ignore[arg-type]
            condition = rule.condition or {}
            trigger_q = ques_by_id.get(trigger_id)
            trigger_page = page_id_by_ques.get(trigger_id)
            trigger_is_for = trigger_page is not None and trigger_page in for_page_ids

            for action in rule.then_list:
                then_type = _num(action.then_type)
                then_vis = int(_num(action.then_visibility)) if math.isfinite(_num(action.then_visibility)) else 0
                if then_type == THEN_TYPE_PAGE:
                    # Page-level rules are global: the trigger is read at page key 1.
                    ans = _trigger_answer_at(trigger_id, 1, answers, prev_ques, trigger_page)
                    cond = evaluate_condition(trigger_q, condition, ans)
                    (page_asserted if cond else page_fallback)[action.then_id] = then_vis if cond else _flip(then_vis)
                    continue
                if then_type not in (THEN_TYPE_QUESTION, THEN_TYPE_OPTION):
                    continue
                if then_type == THEN_TYPE_QUESTION:
                    target_page = page_id_by_ques.get(action.then_id)
                else:
                    owner = ques_of_option.get(action.then_id)
                    if owner is None:
                        continue
                    target_page = page_id_by_ques.get(owner)
                if target_page is None:
                    continue
                for k in _keys(page_keys_by_page_id, target_page):
                    k_eval = k if trigger_is_for else 1
                    ans = _trigger_answer_at(trigger_id, k_eval, answers, prev_ques, trigger_page)
                    cond = evaluate_condition(trigger_q, condition, ans)
                    key = (int(then_type), action.then_id, k)
                    (tab_asserted if cond else tab_fallback)[key] = then_vis if cond else _flip(then_vis)

        page_effective = {**page_fallback, **page_asserted}
        tab_effective = {**tab_fallback, **tab_asserted}

        next_pages = {page_id for page_id, vis in page_effective.items() if vis == VIS_HIDE}
        next_ques: Dict[TabKey, Set[int]] = {}
        next_options: Dict[TabKey, Dict[int, Set[int]]] = {}
        for (then_type, then_id, k), vis in tab_effective.items():
            if vis != VIS_HIDE:
                continue
            if then_type == THEN_TYPE_QUESTION:
                page_id = page_id_by_ques.get(then_id)
                if page_id is None:
                    continue
                next_ques.setdefault(tab_key(page_id, k), set()).add(then_id)
            elif then_type == THEN_TYPE_OPTION:
                owner = ques_of_option.get(then_id)
                if owner is None:
                    continue
                page_id = page_id_by_ques.get(owner)
                if page_id is None:
                    continue
                next_options.setdefault(tab_key(page_id, k), {}).setdefault(owner, set()).add(then_id)

        # A hidden page hides every question on it, on every entry.
        for page_id in next_pages:
            page_ques = ques_ids_by_page.get(page_id) or []
            if not page_ques:
                continue
            for k in _keys(page_keys_by_page_id, page_id):
                next_ques.setdefault(tab_key(page_id, k), set()).update(page_ques)

        if next_ques == prev_ques and next_pages == prev_pages and next_options == prev_options:
            return EvaluationResult(next_pages, next_ques, next_options)
        prev_pages, prev_ques, prev_options = next_pages, next_ques, next_options

    return EvaluationResult(prev_pages, prev_ques, prev_options)


# ---------------------------------------------------------------- AT_LEAST / UNIQUE
@dataclass
class HiddenTarget:
    ques_id: int
    page_id: int
    question_position: int
    page_position: int


@dataclass
class AtLeastViolation:
    rule_id: int
    required: int
    filled: int
    visible_targets_by_tab: Dict[TabKey, List[int]]
    hidden_targets: List[HiddenTarget]


@dataclass
class UniqueViolation:
    rule_id: int
    flagged_by_tab: Dict[TabKey, Set[int]]
    question_ids: List[int]


def evaluate_at_least_rules(
    *,
    rules: List[Rule],
    answers: Answers,
    form: Form,
    for_page_ids: Set[int],
    page_keys_by_page_id: Mapping[int, List[int]],
    hidden_ques_ids_by_tab: Mapping[TabKey, Set[int]],
) -> List[AtLeastViolation]:
    if not any(_num(r.type) == RULE_TYPE_AT_LEAST for r in rules):
        return []
    ques_by_id = form.question_by_id()
    page_position = {p.id: p.position for p in form.page_list}
    out: List[AtLeastViolation] = []
    for rule in rules:
        if _num(rule.type) != RULE_TYPE_AT_LEAST:
            continue
        # `Number(rule.condition?.cond_val_s ?? "")`
        raw_min = (rule.condition or {}).get("cond_val_s")
        minimum = js_number("" if raw_min is None else raw_min)
        if not math.isfinite(minimum) or minimum <= 0:
            continue
        targets = [
            ques_by_id[t.then_id]
            for t in rule.then_list
            if _num(t.then_type) == THEN_TYPE_QUESTION and t.then_id in ques_by_id
        ]
        if not targets:
            continue
        iteration_page = next((q.page_id for q in targets if q.page_id in for_page_ids), None)
        iteration_keys = _keys(page_keys_by_page_id, iteration_page) if iteration_page is not None else [1]
        for k in iteration_keys:
            filled = 0
            visible = 0
            by_tab: Dict[TabKey, List[int]] = {}
            hidden: List[HiddenTarget] = []
            for question in targets:
                pk = k if question.page_id == iteration_page else 1
                tk = tab_key(question.page_id, pk)
                if question.id in hidden_ques_ids_by_tab.get(tk, ()):
                    hidden.append(
                        HiddenTarget(
                            question.id, question.page_id, question.position, page_position.get(question.page_id, 0)
                        )
                    )
                    continue
                visible += 1
                by_tab.setdefault(tk, []).append(question.id)
                if not is_empty_answer(get_answer(answers, question.id, pk)):
                    filled += 1
            if visible == 0:
                continue
            required = min(minimum, visible)
            if filled < required:
                out.append(
                    AtLeastViolation(
                        rule.id, int(required) if float(required).is_integer() else required, filled, by_tab, hidden
                    )
                )  # type: ignore[arg-type]
    return out


def _unique_targets(rule: Rule, ques_by_id: Mapping[int, Question]) -> List[Question]:
    seen: List[int] = []
    for then in rule.then_list:
        if _num(then.then_type) == THEN_TYPE_QUESTION and then.then_id not in seen:
            seen.append(then.then_id)
    return [ques_by_id[i] for i in seen if i in ques_by_id]


def evaluate_unique_rules(
    *,
    rules: List[Rule],
    answers: Answers,
    form: Form,
    page_keys_by_page_id: Mapping[int, List[int]],
    hidden_ques_ids_by_tab: Mapping[TabKey, Set[int]],
) -> List[UniqueViolation]:
    """UNIQUE within one response: the targets form one tuple per FOR entry and
    two entries holding the same tuple collide. Values of different questions
    are never compared, so a rule on non-repeating pages can't fail here."""
    if not any(_num(r.type) == RULE_TYPE_UNIQUE for r in rules):
        return []
    ques_by_id = form.question_by_id()
    out: List[UniqueViolation] = []
    for rule in rules:
        if _num(rule.type) != RULE_TYPE_UNIQUE:
            continue
        targets = _unique_targets(rule, ques_by_id)
        if not targets:
            continue
        keys_by_target = [_keys(page_keys_by_page_id, q.page_id) for q in targets]
        iteration_keys = sorted({k for keys in keys_by_target for k in keys})
        tuples_by_value: Dict[Tuple[str, ...], List[List[Tuple[int, int, int]]]] = {}
        seen_slot_sets: Set[Tuple[int, ...]] = set()
        for k in iteration_keys:
            slots: List[Tuple[int, int, int]] = []  # (ques_id, page_id, page_key)
            item_keys: List[str] = []
            for question, keys in zip(targets, keys_by_target):
                pk = k if k in keys else keys[0]
                value = get_answer(answers, question.id, pk)
                if question.id in hidden_ques_ids_by_tab.get(tab_key(question.page_id, pk), ()) or is_empty_answer(
                    value
                ):
                    break
                slots.append((question.id, question.page_id, pk))
                if question.q_type == QType.DATETIME:
                    item_keys.append(normalize_wall_clock(value) or coerce_string(value))
                else:
                    item_keys.append(coerce_string(value))
            if len(slots) < len(targets):
                continue
            slot_set = tuple(s[2] for s in slots)
            if slot_set in seen_slot_sets:
                continue
            seen_slot_sets.add(slot_set)
            tuples_by_value.setdefault(tuple(item_keys), []).append(slots)

        flagged: Dict[TabKey, Set[int]] = {}
        distinct: List[int] = []
        for colliding in tuples_by_value.values():
            if len(colliding) < 2:
                continue
            for i in range(len(targets)):
                # A slot every colliding tuple shares can't tell them apart.
                if len({slots[i][2] for slots in colliding}) < 2:
                    continue
                for slots in colliding:
                    ques_id, page_id, pk = slots[i]
                    flagged.setdefault(tab_key(page_id, pk), set()).add(ques_id)
                    if ques_id not in distinct:
                        distinct.append(ques_id)
        if flagged:
            out.append(UniqueViolation(rule.id, flagged, distinct))
    return out


def unique_rule_target_groups(rules: Iterable[Rule]) -> List[Tuple[int, List[int]]]:
    """Each UNIQUE rule's QUESTION targets, grouped by rule: ``[(rule_id, ids)]``."""
    out: List[Tuple[int, List[int]]] = []
    for rule in rules:
        if _num(rule.type) != RULE_TYPE_UNIQUE:
            continue
        ids: List[int] = []
        for then in rule.then_list:
            if _num(then.then_type) == THEN_TYPE_QUESTION and then.then_id not in ids:
                ids.append(then.then_id)
        if ids:
            out.append((rule.id, ids))
    return out


# ---------------------------------------------------------------- author-hidden
def is_author_hidden_page(page: Page) -> bool:
    return page.visibility is False


def is_author_hidden_question(question: Question) -> bool:
    return question.attr("visibility") is False
