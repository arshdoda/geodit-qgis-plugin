"""When a CALCULATE slot stops recomputing.

Port of geodit-ui ``runtime/calcFreeze.ts``. Slots are ``(ques_id, page_key)``:

* ``typed`` — the user typed into it this session: an override, kept while it
  holds a value;
* ``stored`` — it was prefilled from the stored response, and no input it reads
  has changed since: kept as stored while it holds a value, so opening a
  response never rewrites a calculation, until an input edit releases it
  (``unfreeze_stored_calcs``), so the saved response can't contradict itself;
* ``cleared`` — the user emptied it: paused, empty, until an input changes.

Everything else recomputes live.
"""

from __future__ import annotations

from typing import AbstractSet, Mapping, NamedTuple, Optional, Set

from .answers import Answers, SlotKey, error_key


class CalcFreezeState(NamedTuple):
    typed: AbstractSet[SlotKey]
    stored: AbstractSet[SlotKey]
    cleared: AbstractSet[SlotKey]


def is_calc_frozen(state: CalcFreezeState, slot: SlotKey, holds_value: bool) -> bool:
    return (holds_value and (slot in state.typed or slot in state.stored)) or slot in state.cleared


def prefilled_slots(answers: Optional[Answers]) -> Set[SlotKey]:
    """Every slot of a prefilled answer set — the ``stored`` freeze at open."""
    return {error_key(ques_id, page_key) for ques_id, by_key in (answers or {}).items() for page_key in by_key}


def unfreeze_stored_calcs(
    stored: AbstractSet[SlotKey],
    edited_id: int,
    page_key: int,
    calc_refs: Mapping[int, AbstractSet[int]],
    page_of: Mapping[int, int],
) -> AbstractSet[SlotKey]:
    """Release the stored calculations that read ``edited_id``, as edited at
    ``page_key``. A calculation reads a question on its own page at its own
    entry, and one on another page at entry 1 (``flatten_for_calc``) — so an
    edit in entry 2 never touches entry 1's stored result. ``calc_refs`` maps
    each CALCULATE question to every question it reads, through calc → calc
    chains. Returns ``stored`` itself when nothing changes."""
    released: Set[SlotKey] = set()
    for slot in stored:
        owner, key = slot
        if owner == edited_id or edited_id not in calc_refs.get(owner, ()):
            continue
        same_page = page_of.get(owner) == page_of.get(edited_id)
        if (key == page_key) if same_page else (page_key == 1):
            released.add(slot)
    return stored - released if released else stored
