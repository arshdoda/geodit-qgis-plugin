"""One open feature form: ``FormRenderer``'s state and submit pipeline, without the UI.

Port of geodit-ui ``components/forms/runtime/FormRenderer.tsx`` as the answer
sheet mounts it (embedded, no draft, no cover screen, PREVIOUS defaults off).
The Qt window renders what this object says and forwards every edit to it, so
the rules, defaults, validation and the save payload are exactly the web's:

* seeding — the stored response, then MANUAL defaults, then SHAPEFILE defaults
  from the feature's attributes (fill-empty-only, each gated on validation);
* ``effective_answers`` — the stored/typed answers overlaid with CALCULATE
  defaults and schema-built unique ids (a multi-pass fixed point; a slot the
  user typed into, or cleared, is left alone);
* presentation — rule-hidden pages / questions / options, plus the author's
  "Visible to surveyor" unless this viewer may see hidden items (then tagged
  and never required);
* ``prepare_submit`` — validate every tab, check AT_LEAST / UNIQUE, build the
  payload (update: only the edited slots; ``clear`` for erased slots and for
  removed repeating-page entries) and the uniqueness groups to probe.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .answers import (
    Answers,
    SlotKey,
    TabKey,
    error_key,
    get_answer,
    has_answer,
    is_empty_answer,
    remap_page_keys,
    same_answer_value,
    set_answer_at,
    tab_key,
)
from .defaults import (
    collect_calc_ref_ques_ids,
    compute_default,
    compute_unique_id,
    is_schema_unique_id,
    resolve_shapefile_default,
    unique_value_own,
)
from .jsnum import is_number, js_number
from .model import DefaultType, Form, Option, Page, QType, Question
from .rules import (
    AtLeastViolation,
    EvaluationResult,
    UniqueViolation,
    compute_for_page_ids,
    evaluate_at_least_rules,
    evaluate_rules,
    evaluate_unique_rules,
    is_author_hidden_page,
    is_author_hidden_question,
    unique_rule_target_groups,
)
from .timezone import normalize_wall_clock
from .unique import UniqueCheckGroup, UniqueCheckItem, build_unique_check_groups
from .validation import flatten_for_calc, storable_range_error, validate_value, visible_options_for


@dataclass
class SessionOptions:
    """What the viewer may do — the answer sheet's props, fed from the
    project's Web access (``DataCaps``)."""

    read_only: bool = False
    allow_read_only_edit: bool = False  # Data write AND "Edit read-only fields"
    can_duplicate_entry: bool = True
    can_remove_entry: bool = True
    show_hidden_pages: bool = False
    show_hidden_questions: bool = False
    submit_changed_only: bool = False  # the UPDATE path: only edited slots


@dataclass(frozen=True)
class Tab:
    page_id: int
    page_key: int
    label: str
    is_for: bool
    page_position: int
    page_name: str
    author_hidden: bool = False

    @property
    def key(self) -> TabKey:
        return tab_key(self.page_id, self.page_key)

    @property
    def heading(self) -> str:
        return f"{self.page_name} · {self.page_key}" if self.is_for else self.page_name

    @property
    def step_name(self) -> str:
        """The stepper's name for the tab (entry 1 of a repeating page is bare)."""
        return f"{self.page_name} · {self.page_key}" if self.is_for and self.page_key > 1 else self.page_name


@dataclass(frozen=True)
class QuestionRef:
    ques_id: int
    page_position: int
    question_position: int
    title: str = ""


@dataclass
class FailureBanner:
    rule_id: int
    kind: str  # "at_least" | "unique" | "unique_server"
    question_refs: List[QuestionRef]
    message: str
    note: Optional[str] = None


@dataclass
class SubmitPayload:
    answers: List[Dict[str, Any]] = field(default_factory=list)
    clear: List[Dict[str, int]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.answers and not self.clear

    def written_slots(self) -> Set[SlotKey]:
        return {error_key(a["ques_id"], a["page_key"]) for a in (*self.answers, *self.clear)}


@dataclass
class PreparedSubmit:
    payload: SubmitPayload
    unique_groups: List[UniqueCheckGroup]


def _js_strict_eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if is_number(a) and is_number(b):
        return a == b
    if a is None and b is None:
        return True
    return a is b


def _default_type_is(question: Question, kind: int, *, strict: bool = False) -> bool:
    value = question.attr("default_type")
    if strict:
        # `q.attributes?.default_type !== DefaultType.MANUAL` — no coercion.
        return is_number(value) and value == kind
    number = js_number(value)
    return number == kind


class FormSession:
    def __init__(
        self,
        form: Form,
        *,
        initial_answers: Optional[Answers] = None,
        feature_attributes: Optional[Mapping[str, Any]] = None,
        options: Optional[SessionOptions] = None,
    ) -> None:
        self.form = form
        self.options = options or SessionOptions()
        self.feature_attributes = feature_attributes
        self.initial_answers: Optional[Answers] = (
            {int(q): {int(k): v for k, v in by_key.items()} for q, by_key in initial_answers.items()}
            if initial_answers is not None
            else None
        )

        self.ques_by_id: Dict[int, Question] = form.question_by_id()
        self.page_by_id: Dict[int, Page] = {p.id: p for p in form.page_list}
        self.ques_page_by_id: Dict[int, int] = {q.id: p.id for p in form.page_list for q in p.ques_list}
        self.page_position_by_id: Dict[int, int] = {p.id: p.position for p in form.page_list}
        self.for_page_ids: Set[int] = compute_for_page_ids(form.rule_list)
        self.unique_target_groups = unique_rule_target_groups(form.rule_list)
        self.calc_refs_by_ques_id = self._calc_refs()
        # The baseline the update diff compares against: the SEEDED answers.
        self.submit_baseline: Optional[Answers] = self.initial_answers
        self.effective_for_page_ids = self._effective_for_pages()

        self.page_keys_by_page_id: Dict[int, List[int]] = self._seed_page_keys()
        # The entries the form LOADED with: a removed entry's rows are erased on
        # save only when the key was here at mount and is gone now.
        self.mounted_page_keys: Dict[int, List[int]] = copy.deepcopy(self.page_keys_by_page_id)

        self._presentation()
        self.answers: Answers = self._build_seed_answers(self.page_keys_by_page_id)
        # A prefilled stored response counts as typed: its CALCULATE slots are
        # frozen, so a save never persists a recomputed value over what was stored.
        self.user_touched: Set[SlotKey] = set()
        for ques_id, by_key in (self.initial_answers or {}).items():
            for page_key in by_key:
                self.user_touched.add(error_key(ques_id, page_key))
        self.errors: Dict[SlotKey, str] = {}
        self.server_unique_keys: Set[SlotKey] = set()
        self.cleared_calc_keys: Set[SlotKey] = set()
        self.validation_attempted = False
        self.tab_index = 0
        self.rule_submit_error: Optional[str] = None
        self._edited = False
        self._cache: Dict[str, Any] = {}

    # ================================================================ setup
    def _calc_refs(self) -> Dict[int, Set[Any]]:
        refs: Dict[int, Set[Any]] = {}
        for question in self.form.questions():
            if _default_type_is(question, DefaultType.CALCULATE):
                refs[question.id] = collect_calc_ref_ques_ids(question)
        grew = True
        while grew:  # expand through calc → calc edges
            grew = False
            for owner_refs in refs.values():
                for ref in list(owner_refs):
                    inner = refs.get(ref)
                    if not inner:
                        continue
                    for x in inner:
                        if x not in owner_refs:
                            owner_refs.add(x)
                            grew = True
        return refs

    def _effective_for_pages(self) -> Set[int]:
        """FOR pages plus any page whose stored response holds an entry ≥ 2 (a
        FOR rule removed after the response was submitted)."""
        if self.initial_answers is None:
            return set(self.for_page_ids)
        out = set(self.for_page_ids)
        for ques_id, by_key in self.initial_answers.items():
            page_id = self.ques_page_by_id.get(ques_id)
            if page_id is None or page_id in out:
                continue
            if any(k >= 2 for k in by_key):
                out.add(page_id)
        return out

    def _seed_page_keys(self) -> Dict[int, List[int]]:
        seed: Dict[int, List[int]] = {p.id: [1] for p in self.form.page_list}
        for ques_id, by_key in (self.initial_answers or {}).items():
            page_id = self.ques_page_by_id.get(ques_id)
            if page_id is None:
                continue
            keys = set(seed.get(page_id, [1]))
            keys.update(k for k in by_key if isinstance(k, int) and k >= 1)
            seed[page_id] = sorted(keys)
        return seed

    def _presentation(self) -> None:
        opts = self.options
        self.concealed_page_ids: Set[int] = (
            set() if opts.show_hidden_pages else {p.id for p in self.form.page_list if is_author_hidden_page(p)}
        )
        self.concealed_ques_ids: Set[int] = (
            set()
            if opts.show_hidden_questions
            else {q.id for q in self.form.questions() if is_author_hidden_question(q)}
        )
        # A REVEALED author-hidden question is never required: surveyors never saw it.
        self.presented_by_id: Dict[int, Question] = {}
        for page in self.form.page_list:
            page_revealed = opts.show_hidden_pages and is_author_hidden_page(page)
            for question in page.ques_list:
                revealed = page_revealed or (opts.show_hidden_questions and is_author_hidden_question(question))
                if revealed and question.attr("mandatory"):
                    self.presented_by_id[question.id] = question.with_attributes(mandatory=False)

    def _build_seed_answers(self, page_keys: Mapping[int, List[int]]) -> Answers:
        initial: Answers = {}
        for ques_id, by_key in (self.initial_answers or {}).items():
            if ques_id not in self.ques_by_id:
                continue
            for page_key, value in by_key.items():
                if not isinstance(page_key, int) or page_key < 1:
                    continue
                if has_answer(initial, ques_id, page_key):
                    continue
                initial = set_answer_at(initial, ques_id, page_key, value)
        # MANUAL defaults for every empty (question, entry).
        for page in self.form.page_list:
            keys = page_keys[page.id] if page.id in page_keys else [1]
            for question in page.ques_list:
                if not _default_type_is(question, DefaultType.MANUAL, strict=True):
                    continue
                for k in keys:
                    if has_answer(initial, question.id, k):
                        continue
                    view = flatten_for_calc(initial, self.form, page.id, k)
                    default = compute_default(question, view, self.form)
                    if default is not None:
                        initial = set_answer_at(initial, question.id, k, default)
        # SHAPEFILE defaults from the feature's attributes, fill-empty-only and
        # gated on validation (attribute data has no authoring gate).
        if self.feature_attributes:
            for page in self.form.page_list:
                keys = page_keys[page.id] if page.id in page_keys else [1]
                for question in page.ques_list:
                    if not _default_type_is(question, DefaultType.SHAPEFILE):
                        continue
                    default = resolve_shapefile_default(question, self.feature_attributes)
                    if default is None or self._validate(question, default) is not None:
                        continue
                    for k in keys:
                        if not has_answer(initial, question.id, k):
                            initial = set_answer_at(initial, question.id, k, default)
        return initial

    # ================================================================ options
    def set_options(self, options: SessionOptions) -> None:
        """The viewer's permissions changed (a caps refresh): re-derive the
        presentation in place — typed values stay."""
        submit_changed_only = self.options.submit_changed_only
        self.options = options
        self.options.submit_changed_only = submit_changed_only
        self._presentation()
        self._invalidate()

    def _validate(self, question: Question, value: Any) -> Optional[str]:
        return validate_value(question, value, read_only_editable=self.options.allow_read_only_edit)

    def _invalidate(self) -> None:
        self._cache.clear()

    def _cached(self, name: str, build):
        if name not in self._cache:
            self._cache[name] = build()
        return self._cache[name]

    # ================================================================ derived
    @property
    def effective_answers(self) -> Answers:
        return self._cached("effective", self._compute_effective)

    def _compute_effective(self) -> Answers:
        work: List[Tuple[Question, int, str]] = []
        for page in self.form.page_list:
            keys = self.page_keys_by_page_id.get(page.id, [1])
            for question in page.ques_list:
                is_calc = _default_type_is(question, DefaultType.CALCULATE)
                is_unique = question.q_type == QType.ID and is_schema_unique_id(question.attributes)
                if not is_calc and not is_unique:
                    continue
                for k in keys:
                    work.append((question, k, "uniqueId" if is_unique else "calc"))
        if not work:
            return self.answers
        current = self.answers
        changed = False
        for _ in range(len(work)):
            pass_changed = False
            # A question hidden by rules is not computed: it stays empty so its
            # value never leaks into other calculations, triggers or the payload.
            hidden = evaluate_rules(
                rules=self.form.rule_list,
                answers=current,
                form=self.form,
                for_page_ids=self.for_page_ids,
                page_keys_by_page_id=self.page_keys_by_page_id,
            ).hidden_ques_ids_by_tab
            for question, pk, kind in work:
                if question.id in hidden.get(tab_key(question.page_id, pk), ()):
                    continue
                slot = error_key(question.id, pk)
                frozen = (
                    slot in self.user_touched and not is_empty_answer(get_answer(self.answers, question.id, pk))
                ) or slot in self.cleared_calc_keys
                if kind != "uniqueId" and frozen:
                    continue
                view = flatten_for_calc(current, self.form, question.page_id, pk, hidden)
                if kind == "uniqueId":
                    stored = unique_value_own(get_answer(self.submit_baseline or {}, question.id, pk))
                    computed = compute_unique_id(question.attributes, view, self.form, None if stored == "" else stored)
                else:
                    computed = compute_default(question, view, self.form)
                if computed is None:
                    continue
                if has_answer(current, question.id, pk) and _js_strict_eq(
                    get_answer(current, question.id, pk), computed
                ):
                    continue
                current = set_answer_at(current, question.id, pk, computed)
                pass_changed = changed = True
            if not pass_changed:
                break
        return current if changed else self.answers

    @property
    def evaluation(self) -> EvaluationResult:
        return self._cached(
            "evaluation",
            lambda: evaluate_rules(
                rules=self.form.rule_list,
                answers=self.effective_answers,
                form=self.form,
                for_page_ids=self.for_page_ids,
                page_keys_by_page_id=self.page_keys_by_page_id,
            ),
        )

    @property
    def at_least_violations(self) -> List[AtLeastViolation]:
        return self._cached(
            "at_least",
            lambda: evaluate_at_least_rules(
                rules=self.form.rule_list,
                answers=self.effective_answers,
                form=self.form,
                for_page_ids=self.for_page_ids,
                page_keys_by_page_id=self.page_keys_by_page_id,
                hidden_ques_ids_by_tab=self.evaluation.hidden_ques_ids_by_tab,
            ),
        )

    @property
    def unique_violations(self) -> List[UniqueViolation]:
        return self._cached(
            "unique",
            lambda: evaluate_unique_rules(
                rules=self.form.rule_list,
                answers=self.effective_answers,
                form=self.form,
                page_keys_by_page_id=self.page_keys_by_page_id,
                hidden_ques_ids_by_tab=self.evaluation.hidden_ques_ids_by_tab,
            ),
        )

    @property
    def visible_pages(self) -> List[Page]:
        return self._cached(
            "visible_pages",
            lambda: [
                p
                for p in self.form.page_list
                if p.id not in self.evaluation.hidden_page_ids and p.id not in self.concealed_page_ids
            ],
        )

    @property
    def tabs(self) -> List[Tab]:
        return self._cached("tabs", self._compute_tabs)

    def _compute_tabs(self) -> List[Tab]:
        out: List[Tab] = []
        for page in self.visible_pages:
            is_for = page.id in self.effective_for_page_ids
            for k in self.page_keys_by_page_id.get(page.id, [1]):
                out.append(
                    Tab(
                        page_id=page.id,
                        page_key=k,
                        label=f"{page.position}. {page.name} - {k}" if is_for else f"{page.position}. {page.name}",
                        is_for=is_for,
                        page_position=page.position,
                        page_name=page.name,
                        author_hidden=is_author_hidden_page(page),
                    )
                )
        return out

    @property
    def has_content(self) -> bool:
        return bool(self.visible_pages) and bool(self.tabs)

    @property
    def show_stepper(self) -> bool:
        return len(self.tabs) > 1 or bool(self.for_page_ids)

    @property
    def safe_tab_index(self) -> int:
        tabs = self.tabs
        return min(max(self.tab_index, 0), len(tabs) - 1) if tabs else 0

    @property
    def current_tab(self) -> Optional[Tab]:
        tabs = self.tabs
        return tabs[self.safe_tab_index] if tabs else None

    @property
    def is_last_tab(self) -> bool:
        return self.safe_tab_index == len(self.tabs) - 1

    def shown_questions(self, page_id: int, page_key: int) -> List[Question]:
        """The questions a tab shows: minus rule-hidden and concealed ones,
        each swapped for its presented copy."""
        page = self.page_by_id.get(page_id)
        if page is None:
            return []
        hidden = self.evaluation.hidden_ques_ids_by_tab.get(tab_key(page_id, page_key), set())
        return [
            self.presented_by_id.get(q.id, q)
            for q in page.ques_list
            if q.id not in hidden and q.id not in self.concealed_ques_ids
        ]

    def tab_questions(self, tab: Tab) -> List[Question]:
        if not any(p.id == tab.page_id for p in self.visible_pages):
            return []
        return self.shown_questions(tab.page_id, tab.page_key)

    def value(self, ques_id: int, page_key: int) -> Any:
        return get_answer(self.effective_answers, ques_id, page_key)

    def visible_options(self, question: Question, page_key: int) -> List[Option]:
        return visible_options_for(question, self.evaluation, tab_key(question.page_id, page_key))

    def question_disabled(self, question: Question) -> bool:
        if self.options.read_only:
            return True
        if question.attr("read_only") and not self.options.allow_read_only_edit:
            return True
        # A schema-built unique id is machine-made: never editable here.
        return question.q_type == QType.ID and is_schema_unique_id(question.attributes)

    def displayed_error(self, question: Question, page_key: int) -> Optional[str]:
        """The error a question shows: re-validated against its CURRENT value,
        but only once validation has run for the slot — or at once for a value
        the server can't store."""
        value = self.value(question.id, page_key)
        stored = self.errors.get(error_key(question.id, page_key))
        unstorable = not self.options.read_only and storable_range_error(question, value) is not None
        return self._validate(question, value) if stored or unstorable else None

    def error_count_by_tab(self) -> Dict[TabKey, int]:
        """Stepper badges: fresh validation of every tab BEFORE the current one."""
        out: Dict[TabKey, int] = {}
        safe = self.safe_tab_index
        for i, tab in enumerate(self.tabs):
            if i >= safe:
                continue
            n = sum(1 for q in self.tab_questions(tab) if self._validate(q, self.value(q.id, tab.page_key)) is not None)
            if n:
                out[tab.key] = n
        return out

    def live_server_flagged(self) -> List[Tuple[int, int, int]]:
        out: List[Tuple[int, int, int]] = []
        evaluation = self.evaluation
        for ques_id, page_key in sorted(self.server_unique_keys):
            question = self.ques_by_id.get(ques_id)
            if question is None:
                continue
            page_id = question.page_id
            if page_id in evaluation.hidden_page_ids or page_id in self.concealed_page_ids:
                continue
            if ques_id in self.concealed_ques_ids or evaluation.question_hidden(page_id, page_key, ques_id):
                continue
            out.append((ques_id, page_id, page_key))
        return out

    def rule_problem_tab_keys(self) -> Set[TabKey]:
        keys: Set[TabKey] = set()
        for v in self.at_least_violations:
            keys.update(v.visible_targets_by_tab.keys())
        for v in self.unique_violations:
            keys.update(v.flagged_by_tab.keys())
        for _, page_id, page_key in self.live_server_flagged():
            keys.add(tab_key(page_id, page_key))
        return keys

    def _ref(self, ques_id: int) -> QuestionRef:
        question = self.ques_by_id.get(ques_id)
        return QuestionRef(
            ques_id,
            self.page_position_by_id.get(question.page_id, 0) if question else 0,
            question.position if question else 0,
            question.label if question else "",
        )

    def banners(self) -> Tuple[List[FailureBanner], Set[int]]:
        """Rule-failure banners for the current tab and the question ids it
        highlights. AT_LEAST is tab-scoped; UNIQUE shows on every tab."""
        banners: List[FailureBanner] = []
        highlighted: Set[int] = set()
        tab = self.current_tab
        if tab is None:
            return banners, highlighted
        current = tab.key

        def sort_key(ref: QuestionRef):
            return (ref.page_position, ref.question_position)

        bannered: Set[int] = set()
        for idx, v in enumerate(self.at_least_violations):
            targets = v.visible_targets_by_tab.get(current)
            if not targets:
                continue
            highlighted.update(targets)
            if v.rule_id in bannered:
                continue
            bannered.add(v.rule_id)
            note = None
            if v.hidden_targets:
                hidden = ", ".join(f"Page {h.page_position} Q{h.question_position}" for h in v.hidden_targets)
                note = f"Note: {len(v.hidden_targets)} target question(s) currently hidden: {hidden}."
            banners.append(
                FailureBanner(
                    v.rule_id,
                    "at_least",
                    [self._ref(i) for i in targets],
                    f"Rule {idx + 1}: At least {v.required} of the highlighted questions must be answered "
                    f"({v.filled}/{v.required} filled).",
                    note,
                )
            )
        bannered = set()
        for idx, v in enumerate(self.unique_violations):
            highlighted.update(v.flagged_by_tab.get(current, ()))
            if v.rule_id in bannered:
                continue
            bannered.add(v.rule_id)
            refs = sorted((self._ref(i) for i in v.question_ids if i in self.ques_by_id), key=sort_key)
            labels: List[str] = []
            for ref in refs:
                label = f"Q{ref.question_position}"
                if label not in labels:
                    labels.append(label)
            banners.append(
                FailureBanner(
                    v.rule_id,
                    "unique",
                    refs,
                    f"Rule {idx + 1}: Unique constraint failed for {', '.join(labels)}. Another entry in this "
                    "response has the same combination — change at least one of the highlighted answers.",
                )
            )
        for question in self.shown_questions(tab.page_id, tab.page_key):
            if error_key(question.id, tab.page_key) in self.server_unique_keys:
                highlighted.add(question.id)
        live = self.live_server_flagged()
        if live:
            flagged_ids = {q for q, _, _ in live}
            scope = set(flagged_ids)
            for _, target_ids in self.unique_target_groups:
                if any(i in flagged_ids for i in target_ids):
                    scope.update(target_ids)
            refs = sorted((self._ref(i) for i in scope if i in self.ques_by_id), key=sort_key)
            banners.append(
                FailureBanner(
                    0,
                    "unique_server",
                    refs,
                    "Already used in another response. The highlighted answer(s) must be unique across submissions.",
                )
            )
        return banners, highlighted

    @property
    def rule_submit_error_shown(self) -> bool:
        return bool(self.rule_submit_error) and bool(
            self.at_least_violations or self.unique_violations or self.server_unique_keys
        )

    @property
    def show_rule_problems(self) -> bool:
        return self.validation_attempted or self.rule_submit_error_shown

    def is_dirty(self) -> bool:
        """Whether a close would lose something: on an update, a non-empty
        diff; on a create, any edit at all."""
        if not self._edited:
            return False
        if self.options.submit_changed_only:
            return not self.build_payload().is_empty()
        return True

    # ================================================================ edits
    def set_answer(self, ques_id: int, value: Any, page_key: Optional[int] = None) -> None:
        if page_key is None:
            tab = self.current_tab
            page_key = tab.page_key if tab else 1
        slot = error_key(ques_id, page_key)
        self._edited = True
        self.user_touched.add(slot)
        self.answers = set_answer_at(self.answers, ques_id, page_key, value)
        self.server_unique_keys.discard(slot)
        # Editing a question a paused calculation reads lifts its pause;
        # clearing a calculated slot pauses it; any other write un-pauses it.
        for key in list(self.cleared_calc_keys):
            owner = key[0]
            if owner != ques_id and ques_id in self.calc_refs_by_ques_id.get(owner, ()):
                self.cleared_calc_keys.discard(key)
        if ques_id in self.calc_refs_by_ques_id and is_empty_answer(value):
            self.cleared_calc_keys.add(slot)
        else:
            self.cleared_calc_keys.discard(slot)
        self._invalidate()
        question = self.presented_by_id.get(ques_id) or self.ques_by_id.get(ques_id)
        if question is None:
            return
        err = self._validate(question, value)
        if err:
            self.errors[slot] = err
        else:
            self.errors.pop(slot, None)

    def can_add_entry(self, page_id: int) -> bool:
        return (
            not self.options.read_only and self.options.can_duplicate_entry and page_id in self.effective_for_page_ids
        )

    def can_remove_entry(self, page_id: int, page_key: int) -> bool:
        return (
            not self.options.read_only
            and self.options.can_remove_entry
            and page_id in self.effective_for_page_ids
            and page_key >= 2
        )

    def add_entry(self, page_id: int) -> bool:
        """The ⧉ action: a new blank entry of a repeating page, which becomes
        the current tab."""
        if not self.can_add_entry(page_id):
            return False
        keys = self.page_keys_by_page_id.get(page_id, [1])
        new_key = (max(keys) if keys else 0) + 1
        self._edited = True
        self.page_keys_by_page_id = {**self.page_keys_by_page_id, page_id: [*keys, new_key]}
        self._invalidate()
        for i, tab in enumerate(self.tabs):
            if tab.page_id == page_id and tab.page_key == new_key:
                self.tab_index = i
                break
        return True

    def remove_entry(self, page_id: int, page_key: int) -> bool:
        """The × action: drop an added entry and resequence the rest to 1..N.
        On the update path the next save erases the rows it stranded."""
        if not self.can_remove_entry(page_id, page_key):
            return False
        page = self.page_by_id.get(page_id)
        keys = self.page_keys_by_page_id.get(page_id, [1])
        if page is None or page_key not in keys:
            return False
        current = self.current_tab
        self._edited = True
        self.server_unique_keys = set()  # slot-keyed flags would point at the wrong tabs
        ques_ids = {q.id for q in page.ques_list}
        remaining = sorted(k for k in keys if k != page_key)
        remap: Dict[int, Optional[int]] = {page_key: None}
        resequenced: List[int] = []
        for i, old in enumerate(remaining):
            remap[old] = i + 1
            resequenced.append(i + 1)
        self.answers = remap_page_keys(self.answers, ques_ids, remap)

        def remap_slot(slot: SlotKey) -> Optional[SlotKey]:
            ques_id, k = slot
            if ques_id not in ques_ids or k not in remap:
                return slot
            mapped = remap[k]
            return None if mapped is None else error_key(ques_id, mapped)

        self.user_touched = {s for s in (remap_slot(k) for k in self.user_touched) if s is not None}
        self.cleared_calc_keys = {s for s in (remap_slot(k) for k in self.cleared_calc_keys) if s is not None}
        self.errors = {remap_slot(k): v for k, v in self.errors.items() if remap_slot(k) is not None}  # type: ignore[misc]
        self.page_keys_by_page_id = {**self.page_keys_by_page_id, page_id: resequenced}
        self._invalidate()
        # Focus: the removed tab falls back to entry 1 of its page; any other
        # tab stays put under its new key.
        removed_active = current is not None and current.page_id == page_id and current.page_key == page_key
        for i, tab in enumerate(self.tabs):
            if removed_active and tab.page_id == page_id and tab.page_key == 1:
                self.tab_index = i
                return True
            if (
                not removed_active
                and current is not None
                and tab.page_id == current.page_id
                and tab.page_key == (remap.get(current.page_key) or current.page_key)
            ):
                self.tab_index = i
                return True
        self.tab_index = min(self.tab_index, max(len(self.tabs) - 1, 0))
        return True

    # ================================================================ navigation
    def validate_tab_questions(self, tab: Tab) -> Dict[SlotKey, str]:
        out: Dict[SlotKey, str] = {}
        for question in self.tab_questions(tab):
            err = self._validate(question, self.value(question.id, tab.page_key))
            if err:
                out[error_key(question.id, tab.page_key)] = err
        return out

    def _store_tab_errors(self, tabs: Sequence[Tab]) -> Dict[SlotKey, str]:
        found: Dict[SlotKey, str] = {}
        checked: List[SlotKey] = []
        for tab in tabs:
            found.update(self.validate_tab_questions(tab))
            checked.extend(error_key(q.id, tab.page_key) for q in self.tab_questions(tab))
        for key in checked:
            self.errors.pop(key, None)
        self.errors.update(found)
        return found

    def go_to_tab(self, index: int) -> None:
        """Stepper clicks and Back. A move back shows the errors of every tab
        it passes; a move forward stays quiet."""
        safe = self.safe_tab_index
        if index == safe or not self.tabs:
            return
        back = index < safe
        if back:
            self._store_tab_errors(self.tabs[index:safe])
        self.validation_attempted = back
        self.tab_index = max(0, min(index, len(self.tabs) - 1))

    def next(self) -> bool:
        """Next: a view-only reader just pages forward; an editor is stopped
        by the current tab's own problems."""
        tabs = self.tabs
        if not tabs:
            return False
        safe = self.safe_tab_index
        if self.options.read_only:
            self.go_to_tab(min(safe + 1, len(tabs) - 1))
            return True
        tab_errors = self._store_tab_errors([tabs[safe]])
        _, highlighted = self.banners()
        if tab_errors or highlighted:
            self.validation_attempted = True
            return False
        self.validation_attempted = False
        self.tab_index = min(safe + 1, len(tabs) - 1)
        return True

    def back(self) -> None:
        self.go_to_tab(max(self.safe_tab_index - 1, 0))

    # ================================================================ submit
    def _page_names(self, tab_keys: Sequence[TabKey]) -> str:
        names: List[str] = []
        for page_id, _ in tab_keys:
            page = self.page_by_id.get(page_id)
            if page is not None and page.name not in names:
                names.append(page.name)
        return ", ".join(names)

    def prepare_submit(self, *, probe_unique: bool = True) -> Optional[PreparedSubmit]:
        """Run the client gates and build the payload; None when a gate stopped
        the save (the errors / banners say why and the current tab moved to
        the first problem)."""
        if self.options.read_only:
            return None
        self.rule_submit_error = None
        all_errors: Dict[SlotKey, str] = {}
        first_error_tab = -1
        for i, tab in enumerate(self.tabs):
            errs = self.validate_tab_questions(tab)
            all_errors.update(errs)
            if first_error_tab < 0 and errs:
                first_error_tab = i
        self.errors = all_errors
        if all_errors:
            self.validation_attempted = True
            if first_error_tab >= 0 and first_error_tab != self.safe_tab_index:
                self.tab_index = first_error_tab
            return None
        if self.at_least_violations or self.unique_violations:
            self.validation_attempted = True
            keys: List[TabKey] = []
            for v in self.at_least_violations:
                keys.extend(v.visible_targets_by_tab.keys())
            for v in self.unique_violations:
                keys.extend(v.flagged_by_tab.keys())
            pages = self._page_names(keys)
            self.rule_submit_error = (
                f"Some rules are not satisfied on {pages}. Review those pages before submitting."
                if pages
                else "Some rules are not satisfied. Review the form before submitting."
            )
            return None
        payload = self.build_payload()
        groups: List[UniqueCheckGroup] = []
        if probe_unique and self.unique_target_groups:
            written = payload.written_slots()
            groups = [
                g
                for g in build_unique_check_groups(
                    answers=self.effective_answers,
                    ques_by_id=self.ques_by_id,
                    target_groups=self.unique_target_groups,
                    page_keys_by_page_id=self.page_keys_by_page_id,
                    hidden_ques_ids_by_tab=self.evaluation.hidden_ques_ids_by_tab,
                )
                if any(error_key(it.ques_id, it.page_key) in written for it in g.items)
            ]
        return PreparedSubmit(payload, groups)

    def build_payload(self) -> SubmitPayload:
        baseline = self.submit_baseline if self.options.submit_changed_only else None
        evaluation = self.evaluation
        out: List[Dict[str, Any]] = []
        cleared: List[Dict[str, int]] = []
        effective = self.effective_answers
        for ques_id in sorted(effective):
            question = self.ques_by_id.get(ques_id)
            if question is None:
                continue
            live_keys = set(self.page_keys_by_page_id.get(question.page_id, [1]))
            is_schema_id = question.q_type == QType.ID and is_schema_unique_id(question.attributes)
            for page_key in sorted(effective[ques_id]):
                value = effective[ques_id][page_key]
                if page_key not in live_keys:
                    continue
                if evaluation.question_hidden(question.page_id, page_key, ques_id):
                    continue
                # A stored schema-built id is never resubmitted (it would renumber the response).
                if (
                    baseline is not None
                    and is_schema_id
                    and unique_value_own(get_answer(baseline, ques_id, page_key)) != ""
                ):
                    continue
                if baseline is not None:
                    present = has_answer(baseline, ques_id, page_key)
                    base = get_answer(baseline, ques_id, page_key)
                    unchanged = is_empty_answer(value) if not present else same_answer_value(base, value)
                    if unchanged:
                        continue
                    if value is None:  # held something, holds nothing now: erase
                        cleared.append({"ques_id": ques_id, "page_key": page_key})
                        continue
                if is_schema_id:
                    submit = unique_value_own(question.attr("unique_value"))
                elif question.q_type == QType.DATETIME:
                    submit = normalize_wall_clock(value) or value
                else:
                    submit = value
                out.append({"ques_id": ques_id, "page_key": page_key, "value": submit})
        # Stored rows the form no longer holds. An entry removed from a repeating
        # page strands the rows of its old (top) key: keys live at mount, gone
        # now. And the removal renumbers the entries after it (an entry added
        # later reuses a freed key), so a live key can lack an answer it stored:
        # seeding copies every stored slot, so only a renumber leaves one out.
        # Rule-hidden slots and schema-built ids keep their stored rows, as above.
        if baseline is not None:
            for ques_id in sorted(baseline):
                question = self.ques_by_id.get(ques_id)
                if question is None:
                    continue
                live_keys = set(self.page_keys_by_page_id.get(question.page_id, [1]))
                mounted = set(self.mounted_page_keys.get(question.page_id, [1]))
                is_schema_id = question.q_type == QType.ID and is_schema_unique_id(question.attributes)
                for page_key in sorted(baseline[ques_id]):
                    if is_empty_answer(baseline[ques_id][page_key]):
                        continue
                    if page_key in live_keys:
                        if (
                            is_schema_id
                            or has_answer(effective, ques_id, page_key)
                            or evaluation.question_hidden(question.page_id, page_key, ques_id)
                        ):
                            continue
                    elif page_key not in mounted:
                        continue
                    cleared.append({"ques_id": ques_id, "page_key": page_key})
        return SubmitPayload(out, cleared)

    def apply_unique_hits(self, hits: Sequence[UniqueCheckItem]) -> None:
        """The probe found combinations already used in another response."""
        if not hits:
            if self.server_unique_keys:
                self.server_unique_keys = set()
                self._invalidate()
            return
        self.server_unique_keys = {error_key(h.ques_id, h.page_key) for h in hits}
        self.validation_attempted = True
        for i, tab in enumerate(self.tabs):
            if any(
                self.ques_by_id.get(h.ques_id)
                and self.ques_by_id[h.ques_id].page_id == tab.page_id
                and h.page_key == tab.page_key
                for h in hits
            ):
                if i != self.safe_tab_index:
                    self.tab_index = i
                break
        names: List[str] = []
        for hit in hits:
            question = self.ques_by_id.get(hit.ques_id)
            page = self.page_by_id.get(question.page_id) if question else None
            if page is not None and page.name not in names:
                names.append(page.name)
        pages = ", ".join(names)
        self.rule_submit_error = (
            f"Some answers already exist in another response — review the highlighted questions on {pages}."
            if pages
            else "Some answers already exist in another response — review the highlighted questions."
        )
        self._invalidate()
