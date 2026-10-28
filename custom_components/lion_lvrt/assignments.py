"""Which cell is in which slot, and the cycles that have run on it.

WHY THIS EXISTS
---------------
The tester knows a slot moved 2871 mAh. It does not know whose cell that
was. An operator scans two barcodes on a phone - one on the slot, one on the
cell - and from then until the next scan every cycle that finishes in that
slot is recorded against the cell's code.

The assignment is therefore *sticky*: it survives a Home Assistant restart,
the tester going offline, and any number of cycles. It is replaced by the
next scan for that slot and cleared explicitly, never implicitly at the end
of a test - a charge, a rest and a discharge are three cycles on one cell,
and dropping the code after the first would misfile the other two.

WHAT IS STORED, AND WHAT IS NOT
-------------------------------
Two things, per tester:

* the current assignment per slot, and
* a bounded tail of finished cycles, newest first.

The tail exists so a dashboard can show the last few runs and so a cycle
scanned *late* can still be attributed (see :meth:`Assignments.relabel`).
It is not an archive: Home Assistant's own recorder holds the event stream,
which is the thing to query for history. This file would otherwise grow
without limit in ``.storage``.

SLOT NUMBERING
--------------
Everything in this module is 0-based, like the rest of the integration.
``parse_slot_code`` is the one place that converts, because a label stuck on
the front panel reads "Slot 1" for slot 0.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "lion_lvrt_assignments"

#: How many finished cycles are kept per tester. Eight slots times a
#: charge/rest/discharge sequence is a handful per test run, so this holds
#: the recent past without letting the file grow without bound.
CYCLE_HISTORY_MAX = 200

#: Anything that can sit between the parts of a scanned label.
_SEPARATORS = re.compile(r"[\s\-_:/.,|]+")
_HEX = re.compile(r"[0-9A-F]+")
#: ``S3``, ``SLOT3`` - the prefixed forms. A bare number is handled separately
#: so the address's own digits can be excluded first.
_SLOT_WORD = re.compile(r"^(?:SLOT|CH|S)([0-9]{1,2})$")
_BARE_NUMBER = re.compile(r"^([0-9]{1,2})$")

#: A Bluetooth address is six octets. Written with or without separators, it
#: is twelve hex digits.
_ADDRESS_DIGITS = 12


class AssignmentError(ValueError):
    """A code could not be parsed or applied; the message says why."""


def parse_slot_code(code: str) -> tuple[str, int]:
    """Decode a scanned slot label into ``(address, slot)``.

    A slot label names a physical socket, so it normally carries both the
    tester's Bluetooth address and the slot number - one bench can hold two
    testers and a bare "3" would be ambiguous. Accepted, case-insensitively::

        e465b810d93a-3          E4:65:B8:10:D9:3A S3
        BTS/e465b810d93a/SLOT3  lion:e465b810d93a:slot-3
        3                       SLOT3

    Without an address the returned address is empty, and the caller decides
    whether it can resolve the tester some other way.

    The number in a label is **1-based**, matching the front panel's
    silk-screen. The returned slot is 0-based, like everything else here.
    """
    text = (code or "").strip()
    if not text:
        raise AssignmentError("the slot code is empty")

    tokens = [t for t in _SEPARATORS.split(text.upper()) if t]
    address, used = _take_address(tokens)
    rest = [t for i, t in enumerate(tokens) if i not in used]

    for token in rest:
        word = _SLOT_WORD.match(token)
        if word is not None:
            return address, _slot_from_number(int(word.group(1)), text)
    for token in rest:
        bare = _BARE_NUMBER.match(token)
        if bare is not None:
            return address, _slot_from_number(int(bare.group(1)), text)

    raise AssignmentError(
        f"slot code {text!r} carries no slot number. Expected something like "
        f"{(address or 'e465b810d93a').lower()}-3 for front-panel slot 3"
    )


def _take_address(tokens: list[str]) -> tuple[str, set[int]]:
    """Find a Bluetooth address and say which tokens it consumed.

    The address may be one token (``E465B810D93A``) or six (``E4 65 B8 10
    D9 3A``), so consecutive hex tokens are joined until they make twelve
    digits. Returning the indices matters: the slot number is looked for in
    what is left, so the address's own digits can never be read as a slot.
    """
    for start in range(len(tokens)):
        joined = ""
        for end in range(start, len(tokens)):
            if not _HEX.fullmatch(tokens[end]):
                break
            joined += tokens[end]
            if len(joined) == _ADDRESS_DIGITS:
                return joined, set(range(start, end + 1))
            if len(joined) > _ADDRESS_DIGITS:
                break
    return "", set()


def _slot_from_number(number: int, text: str) -> int:
    if number < 1:
        raise AssignmentError(
            f"slot code {text!r} names slot {number}; panel slots start at 1"
        )
    return number - 1


@dataclass
class Assignment:
    """A cell code bound to one slot."""

    slot: int
    code: str
    assigned_at: str
    #: The slot label that was scanned, when one was. Kept for the audit
    #: trail: it says which socket the operator actually scanned, which is
    #: worth having when a label is on the wrong socket.
    slot_code: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Assignments:
    """One tester's assignments and its recent cycles.

    Pure data with no I/O: :class:`AssignmentStore` persists it. That split
    keeps the attribution rules testable without Home Assistant's storage
    helper.
    """

    slots: dict[int, Assignment] = field(default_factory=dict)
    #: Newest first.
    cycles: list[dict] = field(default_factory=list)

    # --- assignment -------------------------------------------------------

    def assign(
        self,
        slot: int,
        code: str,
        *,
        when: str,
        slot_code: str = "",
        note: str = "",
    ) -> Assignment:
        code = (code or "").strip()
        if not code:
            raise AssignmentError("the cell code is empty")
        assignment = Assignment(
            slot=slot, code=code, assigned_at=when, slot_code=slot_code, note=note
        )
        self.slots[slot] = assignment
        return assignment

    def clear(self, slot: int) -> Assignment | None:
        return self.slots.pop(slot, None)

    def code_for(self, slot: int) -> str:
        assignment = self.slots.get(slot)
        return assignment.code if assignment else ""

    def get(self, slot: int) -> Assignment | None:
        return self.slots.get(slot)

    # --- cycles -----------------------------------------------------------

    def record(self, cycle: dict) -> dict:
        """Add a finished cycle, dropping the oldest once the tail is full."""
        self.cycles.insert(0, cycle)
        del self.cycles[CYCLE_HISTORY_MAX:]
        return cycle

    def last_cycle(self, slot: int) -> dict | None:
        for cycle in self.cycles:
            if cycle.get("slot") == slot:
                return cycle
        return None

    def relabel(self, slot: int, code: str, *, limit: int = 1) -> list[dict]:
        """Attach ``code`` to the most recent unattributed cycles of ``slot``.

        For the operator who runs the test and scans the cell afterwards.
        Only cycles with no code are touched, so a late scan can never
        overwrite an attribution that was already correct.
        """
        changed: list[dict] = []
        for cycle in self.cycles:
            if len(changed) >= limit:
                break
            if cycle.get("slot") != slot or cycle.get("code"):
                continue
            cycle["code"] = code
            cycle["code_assigned_late"] = True
            changed.append(cycle)
        return changed

    # --- persistence ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "assignments": [a.as_dict() for a in self.slots.values()],
            "cycles": self.cycles,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Assignments":
        """Rebuild from stored data, ignoring anything malformed.

        A store that cannot be read must not stop the integration loading:
        losing an assignment costs a rescan, while failing to set up costs
        the whole tester.
        """
        out = cls()
        if not isinstance(data, dict):
            return out
        for raw in data.get("assignments") or []:
            try:
                out.slots[int(raw["slot"])] = Assignment(
                    slot=int(raw["slot"]),
                    code=str(raw["code"]),
                    assigned_at=str(raw.get("assigned_at", "")),
                    slot_code=str(raw.get("slot_code", "")),
                    note=str(raw.get("note", "")),
                )
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignoring a malformed stored assignment: %r", raw)
        cycles = data.get("cycles")
        if isinstance(cycles, list):
            out.cycles = [c for c in cycles if isinstance(c, dict)][:CYCLE_HISTORY_MAX]
        return out


class AssignmentStore:
    """:class:`Assignments` backed by Home Assistant's storage helper.

    Saves are delayed and coalesced by ``Store.async_delay_save``: a cycle
    ending on all eight slots at once is one write, not eight, and a slot
    rescanned twice in a second does not write twice.
    """

    #: Seconds to coalesce writes over.
    SAVE_DELAY = 5

    def __init__(self, hass, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store  # noqa: PLC0415

        self.data = Assignments()
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}")

    async def async_load(self) -> Assignments:
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a bad store must not block setup
            _LOGGER.warning("Could not read stored slot assignments: %s", err)
            raw = None
        self.data = Assignments.from_json(raw)
        return self.data

    def save(self) -> None:
        self._store.async_delay_save(self.data.to_json, self.SAVE_DELAY)

    async def async_remove(self) -> None:
        await self._store.async_remove()
