"""The form payload (``GET /forms/{proj}/form-data/{form_id}``).

Port of geodit-ui ``services/form/detail/type.ts`` (``FormDetails``,
``PageList``, ``QuesList``, ``OptList``, ``RuleData``) and the enums in
``types/questions.ts``. Pages and questions keep the payload's order — the web
renders them in array order. A question's ``attributes`` stay a plain dict: the
server stores them as free-form JSON and never enforces a schema, so every
reader tolerates missing or odd values, as the web does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional

from .jsnum import is_number, js_number


class QType:
    """``QuestionType`` (``types/questions.ts``; the numbers in
    ``docs/question-system.md`` are stale)."""

    PARAGRAPH = 2
    DATETIME = 3
    LOCATION = 4
    PHONE = 5
    TEXT = 20
    IDENTITY = 21
    EMAIL = 22
    MULTIPLE_CHOICE = 23
    ID = 30
    NUMBER = 31
    RATING = 32
    COUNTER = 33
    DROPDOWN = 34
    DECIMAL = 41
    MEASUREMENT = 42
    IMAGE = 51
    DOCUMENT = 52
    AUDIO = 53
    VIDEO = 54
    SIGNATURE = 55


MEDIA_QTYPES = frozenset({QType.IMAGE, QType.DOCUMENT, QType.AUDIO, QType.VIDEO, QType.SIGNATURE})
CHOICE_QTYPES = frozenset({QType.DROPDOWN, QType.MULTIPLE_CHOICE})


class DefaultType:
    NONE = 0
    MANUAL = 1
    PREVIOUS = 2
    SHAPEFILE = 3
    CALCULATE = 4


class IdentityType:
    AADHAAR_CARD = 1
    PAN_CARD = 2
    DRIVING_LICENSE = 3
    VOTER_ID = 4
    PASSPORT = 5


MANUAL_ENTRY_OPTION = "_manual_"  # `ManualEntryOption` (services/form/detail/constant.ts)
TEXT_ANSWER_MAX_LEN = 255
PARAGRAPH_ANSWER_MAX_LEN = 2000


def _int(value: Any, default: int = 0) -> int:
    number = js_number(value) if not is_number(value) else float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±∞
        return default
    return int(number)


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    number = js_number(value) if not is_number(value) else float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number)


@dataclass(eq=False)
class Option:
    id: int
    ques_id: int
    value: str
    position: int = 0


@dataclass(eq=False)
class Question:
    id: int
    page_id: int
    q_type: int
    title: str = ""
    question: str = ""
    position: int = 0
    attributes: Dict[str, Any] = field(default_factory=dict)
    opt_list: List[Option] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What the web shows above the input (`BaseQuestion`): the question
        text, else the title."""
        return (self.question or "").strip() or (self.title or "").strip()

    def attr(self, key: str, default: Any = None) -> Any:
        return self.attributes.get(key, default) if isinstance(self.attributes, Mapping) else default

    def with_attributes(self, **changes: Any) -> Question:
        return replace(self, attributes={**(self.attributes or {}), **changes})


@dataclass(eq=False)
class Page:
    id: int
    name: str
    position: int
    ques_list: List[Question] = field(default_factory=list)
    # The builder's page-level "Visible to surveyor". Absent (an older backend)
    # means visible.
    visibility: Optional[bool] = None


@dataclass(eq=False)
class RuleThen:
    id: int
    then_type: int
    then_visibility: int
    then_id: int


@dataclass(eq=False)
class Rule:
    id: int
    type: int
    position: int
    ques_id: Optional[int]
    # `{operator, cond_val_s, cond_val_e}` as sent (values kept raw: the engine
    # coerces them exactly where the web does).
    condition: Optional[Dict[str, Any]]
    then_list: List[RuleThen] = field(default_factory=list)


@dataclass(eq=False)
class Form:
    id: int
    name: str
    page_list: List[Page]
    rule_list: List[Rule]
    unique_ques_id: Optional[int] = None

    def questions(self) -> List[Question]:
        return [q for page in self.page_list for q in page.ques_list]

    def question_by_id(self) -> Dict[int, Question]:
        return {q.id: q for q in self.questions()}


def _parse_option(raw: Mapping, ques_id: int) -> Option:
    return Option(
        id=_int(raw.get("id")),
        ques_id=_int(raw.get("ques_id"), ques_id),
        value="" if raw.get("value") is None else str(raw.get("value")),
        position=_int(raw.get("position")),
    )


def _parse_question(raw: Mapping, page_id: int, index: int) -> Question:
    attributes = raw.get("attributes")
    ques_id = _int(raw.get("id"))
    position = raw.get("position")
    return Question(
        id=ques_id,
        page_id=_int(raw.get("page_id"), page_id),
        q_type=_int(raw.get("q_type")),
        title=str(raw.get("title") or ""),
        question=str(raw.get("question") or ""),
        position=_int(position) if position is not None else index + 1,
        attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
        opt_list=[_parse_option(o, ques_id) for o in (raw.get("opt_list") or []) if isinstance(o, Mapping)],
    )


def _parse_rule(raw: Mapping) -> Rule:
    condition = raw.get("condition")
    return Rule(
        id=_int(raw.get("id")),
        type=_int(raw.get("type")),
        position=_int(raw.get("position")),
        ques_id=_opt_int(raw.get("ques_id")),
        condition=dict(condition) if isinstance(condition, Mapping) else None,
        then_list=[
            RuleThen(
                id=_int(t.get("id")),
                then_type=_int(t.get("then_type")),
                then_visibility=_int(t.get("then_visibility")),
                then_id=_int(t.get("then_id")),
            )
            for t in (raw.get("then_list") or [])
            if isinstance(t, Mapping)
        ],
    )


def parse_form(payload: Mapping) -> Form:
    pages: List[Page] = []
    for raw_page in payload.get("page_list") or []:
        if not isinstance(raw_page, Mapping):
            continue
        page_id = _int(raw_page.get("id"))
        visibility = raw_page.get("visibility")
        pages.append(
            Page(
                id=page_id,
                name=str(raw_page.get("name") or ""),
                position=_int(raw_page.get("position")),
                visibility=visibility if isinstance(visibility, bool) else None,
                ques_list=[
                    _parse_question(q, page_id, i)
                    for i, q in enumerate(raw_page.get("ques_list") or [])
                    if isinstance(q, Mapping)
                ],
            )
        )
    return Form(
        id=_int(payload.get("id")),
        name=str(payload.get("name") or ""),
        page_list=pages,
        rule_list=[_parse_rule(r) for r in (payload.get("rule_list") or []) if isinstance(r, Mapping)],
        unique_ques_id=_opt_int(payload.get("unique_ques_id")),
    )
