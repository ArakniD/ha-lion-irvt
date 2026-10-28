"""Detecting a finished charge or discharge, and the figures it moved.

WHAT COUNTS AS A COMPLETED CYCLE
--------------------------------
The unit never announces "the charge has finished". It publishes a status
word and two pairs of accumulators - one per direction, each zeroed only when
that direction *starts*. A cycle is therefore inferred: a slot that was
running in one direction and is no longer running in that direction has
finished a cycle, and the accumulator for the direction it just left holds
the final figures.

Three cases that look like an ending, and whether they are:

* **A pause is not an ending.** A paused slot drops RUNNING but keeps its
  direction bit and freezes its counters, so a resume continues the same
  cycle. The watcher holds the segment open.
* **A phase change IS an ending.** A characterisation runs charge, rest,
  discharge. The rest between them ends the charge, which is exactly the
  event an operator wants - the charge figures are final at that moment.
* **Charge straight into discharge.** The direction changes with no idle
  sample in between. The old segment closes and the new one opens in the
  same observation.

Counters are read from the sample taken *after* the direction ended, because
they are not cleared on stop and that sample is the freshest. The peak seen
while running is kept as a floor: if a new cycle in the same direction began
before this code saw the gap, the counters would have been zeroed, and the
peak is then the only record of what the finished cycle moved.

NO HOME ASSISTANT HERE
----------------------
This module imports nothing from Home Assistant, so the detection logic runs
under any Python 3.10+ the way the protocol and transport layers do. The
storage side is in :mod:`.assignments` and the plumbing in
:mod:`.coordinator`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import SlotFault, SlotState, StatusBit
from .protocol.ble import SlotStatus

DIRECTION_CHARGE = "charge"
DIRECTION_DISCHARGE = "discharge"

#: How a cycle ended. Only ``complete`` and ``ended`` mean the unit decided
#: it was finished; the others were cut short by something.
OUTCOME_COMPLETE = "complete"
OUTCOME_ENDED = "ended"
OUTCOME_PHASE = "phase"
OUTCOME_STOPPED = "stopped"
OUTCOME_ABORTED = "aborted"
OUTCOME_FAULT = "fault"


def normalise_address(value: str | None) -> str:
    """Reduce a Bluetooth address (or any identifier) to a comparable key.

    A barcode carries ``e465b810d93a``; Home Assistant stores
    ``E4:65:B8:10:D9:3A``; an operator may type either, with or without
    separators. Dropping everything that is not alphanumeric and
    upper-casing makes all of them one string.
    """
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isalnum()).upper()


def direction_of(status: SlotStatus) -> str | None:
    """Which way current is flowing right now, or None if it is not.

    Both properties require RUNNING, so a paused slot reports None here even
    though it keeps its direction bit - which is why a caller must check
    :attr:`SlotStatus.is_paused` before reading None as an ending.
    """
    if status.is_charging:
        return DIRECTION_CHARGE
    if status.is_discharging:
        return DIRECTION_DISCHARGE
    return None


def _outcome(status: SlotStatus, replaced_by: str | None) -> str:
    if status.fault is not SlotFault.NONE:
        return OUTCOME_FAULT
    if replaced_by is not None:
        return OUTCOME_PHASE
    if status.state is SlotState.ABORTED:
        return OUTCOME_ABORTED
    if status.state is SlotState.COMPLETE:
        return OUTCOME_COMPLETE
    if status.status_bits & StatusBit.FINISHED:
        return OUTCOME_ENDED
    if status.state.is_active:
        # Still sequencing: this was one phase of a longer test.
        return OUTCOME_PHASE
    return OUTCOME_STOPPED


def _counters(status: SlotStatus, direction: str) -> tuple[float, float, float]:
    """The unit's own mAh / mWh / seconds for one direction."""
    if direction == DIRECTION_CHARGE:
        return (
            status.bts_charge_mah,
            status.bts_charge_mwh,
            status.bts_charge_seconds,
        )
    return (
        status.bts_discharge_mah,
        status.bts_discharge_mwh,
        status.bts_discharge_seconds,
    )


@dataclass
class _Segment:
    """One run in one direction, while it is still open."""

    direction: str
    voltage_start_v: float
    voltage_min_v: float
    voltage_max_v: float
    temp_max_c: float
    current_max_a: float
    samples: int = 0
    #: Highest accumulator readings seen while running, used as a floor when
    #: the counters have already been zeroed by a following cycle.
    peak_mah: float = 0.0
    peak_mwh: float = 0.0
    peak_seconds: float = 0.0
    #: The proxy's own integration, kept only as a fallback for a unit whose
    #: BTS accumulators are not populated - see _close().
    peak_live_mah: float = 0.0
    peak_live_mwh: float = 0.0
    #: The last sample taken while the slot was still running. The closing
    #: sample has already collapsed to no current and a rested voltage, so
    #: the end-of-cycle readings come from this one.
    last: SlotStatus | None = field(default=None, repr=False)

    def sample(self, status: SlotStatus) -> None:
        self.samples += 1
        self.voltage_min_v = min(self.voltage_min_v, status.voltage_v)
        self.voltage_max_v = max(self.voltage_max_v, status.voltage_v)
        self.temp_max_c = max(self.temp_max_c, status.temp_c)
        self.current_max_a = max(self.current_max_a, abs(status.current_a))
        mah, mwh, seconds = _counters(status, self.direction)
        self.peak_mah = max(self.peak_mah, mah)
        self.peak_mwh = max(self.peak_mwh, mwh)
        self.peak_seconds = max(self.peak_seconds, seconds)
        self.peak_live_mah = max(self.peak_live_mah, status.live_mah)
        self.peak_live_mwh = max(self.peak_live_mwh, status.live_mwh)
        self.last = status


def _open(status: SlotStatus, direction: str) -> _Segment:
    segment = _Segment(
        direction=direction,
        voltage_start_v=status.voltage_v,
        voltage_min_v=status.voltage_v,
        voltage_max_v=status.voltage_v,
        temp_max_c=status.temp_c,
        current_max_a=abs(status.current_a),
    )
    segment.sample(status)
    return segment


class CycleWatcher:
    """Turns a stream of snapshots into one record per finished cycle.

    Stateful and not thread-safe, so it is fed from one place. The
    coordinator feeds it from both the poll and the notification path: a
    cycle that ends between polls should be recorded when the push reports
    it rather than up to a poll interval later.
    """

    def __init__(self) -> None:
        self._open: dict[int, _Segment] = {}

    def observe(self, snapshot) -> list[dict]:
        """Record this snapshot; return a record per cycle that just ended."""
        finished: list[dict] = []
        stats_live = bool(getattr(snapshot.unit, "stats_live", True))
        for slot in sorted(snapshot.slots):
            record = self._observe_slot(slot, snapshot.slots[slot], stats_live)
            if record is not None:
                finished.append(record)
        return finished

    def _observe_slot(
        self, slot: int, status: SlotStatus, stats_live: bool
    ) -> dict | None:
        segment = self._open.get(slot)
        direction = direction_of(status)

        if direction is not None:
            if segment is not None and segment.direction == direction:
                segment.sample(status)
                return None
            # A new direction: close the old one, then open the new.
            record = (
                self._close(slot, segment, status, replaced_by=direction,
                            stats_live=stats_live)
                if segment is not None
                else None
            )
            self._open[slot] = _open(status, direction)
            return record

        if segment is None:
            return None
        if status.is_paused:
            # Frozen, not finished: the counters hold and a resume continues
            # this same cycle.
            return None
        return self._close(
            slot, segment, status, replaced_by=None, stats_live=stats_live
        )

    def _close(
        self,
        slot: int,
        segment: _Segment,
        status: SlotStatus,
        *,
        replaced_by: str | None,
        stats_live: bool = True,
    ) -> dict:
        self._open.pop(slot, None)
        mah, mwh, seconds = _counters(status, segment.direction)
        mah = max(mah, segment.peak_mah)
        mwh = max(mwh, segment.peak_mwh)

        # The unit reports whether its own accumulators are populated. On a
        # build that never filled them the proxy integrates the current
        # itself, and that figure - not a confident-looking 0.0 - is what
        # the cycle moved. `counter_source` says which one this is, because
        # the two are measured differently and should not be compared.
        source = "bts"
        if not stats_live and mah <= 0.0:
            mah = max(status.live_mah, segment.peak_live_mah)
            mwh = max(status.live_mwh, segment.peak_live_mwh)
            source = "proxy"

        # The last running sample is what the cell was doing under load; the
        # closing sample may already be at rest.
        final = segment.last if segment.last is not None else status
        return {
            "slot": slot,
            "slot_number": slot + 1,
            "direction": segment.direction,
            "outcome": _outcome(status, replaced_by),
            "mah": round(mah, 1),
            "mwh": round(mwh, 1),
            "seconds": round(max(seconds, segment.peak_seconds), 1),
            "counter_source": source,
            "voltage_start_v": round(segment.voltage_start_v, 3),
            "voltage_end_v": round(final.voltage_v, 3),
            "voltage_min_v": round(segment.voltage_min_v, 3),
            "voltage_max_v": round(segment.voltage_max_v, 3),
            "voltage_rested_v": round(status.voltage_v, 3),
            "current_max_a": round(segment.current_max_a, 3),
            "current_end_a": round(final.current_a, 3),
            "temp_max_c": round(segment.temp_max_c, 1),
            "temp_end_c": round(final.temp_c, 1),
            # The engine's own progress against the configured capacity, as
            # it stood on the last running sample. Can exceed 100% on a cell
            # above its rating, and is not clamped - that overshoot is a
            # result, not an error.
            "progress": round(final.progress * 100.0, 1),
            "state": status.state.name.lower(),
            "fault": status.fault.name.lower(),
            "status_bits": status.status_bits,
            "samples": segment.samples,
        }
