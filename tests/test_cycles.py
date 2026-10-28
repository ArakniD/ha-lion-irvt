"""Completed-cycle detection and cell-code assignment.

The tester never says "the charge has finished": a cycle is inferred from
the status word and the per-direction accumulators. These tests pin the
three cases that decide whether the log is trustworthy - a pause is not an
ending, a phase change is, and a direction reversal closes one cycle and
opens the next - and the barcode rules that decide which cell a finished
cycle is filed under.

No Home Assistant here: both modules under test are deliberately free of it
so they run on any Python 3.10+.
"""

from __future__ import annotations

import pytest

from custom_components.lion_lvrt.assignments import (
    CYCLE_HISTORY_MAX,
    Assignment,
    AssignmentError,
    Assignments,
    parse_slot_code,
)
from custom_components.lion_lvrt.const import SlotFault, SlotState, StatusBit
from custom_components.lion_lvrt.cycles import (
    CycleWatcher,
    direction_of,
    normalise_address,
)
from custom_components.lion_lvrt.protocol.ble import SlotStatus
from custom_components.lion_lvrt.transport.base import DeviceSnapshot


def status(
    slot: int = 0,
    *,
    bits: int = 0,
    state: SlotState = SlotState.IDLE,
    fault: SlotFault = SlotFault.NONE,
    voltage: float = 3.7,
    current: float = 0.0,
    temp: float = 25.0,
    charge_mah: float = 0.0,
    charge_mwh: float = 0.0,
    charge_s: float = 0.0,
    discharge_mah: float = 0.0,
    discharge_mwh: float = 0.0,
    discharge_s: float = 0.0,
    live_mah: float = 0.0,
    live_mwh: float = 0.0,
) -> SlotStatus:
    return SlotStatus(
        slot=slot,
        state=state,
        fault=fault,
        configured=True,
        voltage_v=voltage,
        current_a=current,
        temp_c=temp,
        live_mah=live_mah,
        live_mwh=live_mwh,
        progress=0.0,
        elapsed_s=0,
        state_elapsed_s=0,
        status_bits=bits,
        bts_charge_mah=charge_mah,
        bts_charge_mwh=charge_mwh,
        bts_charge_seconds=charge_s,
        bts_discharge_mah=discharge_mah,
        bts_discharge_mwh=discharge_mwh,
        bts_discharge_seconds=discharge_s,
    )


RUNNING_CHARGE = int(StatusBit.RUNNING | StatusBit.CHARGING)
RUNNING_DISCHARGE = int(StatusBit.RUNNING | StatusBit.DISCHARGING)
PAUSED_DISCHARGE = int(StatusBit.PAUSED | StatusBit.DISCHARGING)


def snap(*slots: SlotStatus, stats_live: bool = True) -> DeviceSnapshot:
    from custom_components.lion_lvrt.protocol.ble import UnitStatus
    from custom_components.lion_lvrt.const import UnitState

    unit = UnitStatus(
        version=8,
        slot_count=8,
        online=True,
        unit_state=UnitState.INPUT_OK,
        trip_status=0,
        input_voltage_v=24.0,
        uptime_s=0,
        stats_live=stats_live,
        wifi_connected=False,
        watchdog_timeout_s=30.0,
    )
    return DeviceSnapshot(unit=unit, slots={s.slot: s for s in slots})


# --- direction --------------------------------------------------------------


def test_direction_needs_running() -> None:
    """A paused slot keeps its direction bit but is not moving current."""
    assert direction_of(status(bits=RUNNING_CHARGE)) == "charge"
    assert direction_of(status(bits=RUNNING_DISCHARGE)) == "discharge"
    assert direction_of(status(bits=PAUSED_DISCHARGE)) is None
    assert direction_of(status(bits=0)) is None


# --- cycle detection --------------------------------------------------------


def test_a_discharge_that_stops_is_one_cycle() -> None:
    watcher = CycleWatcher()
    assert watcher.observe(snap(status(bits=RUNNING_DISCHARGE, current=-2.5))) == []
    watcher.observe(
        snap(
            status(
                bits=RUNNING_DISCHARGE,
                current=-2.5,
                voltage=3.4,
                discharge_mah=1500.0,
                discharge_mwh=5400.0,
                discharge_s=1800.0,
            )
        )
    )
    done = watcher.observe(
        snap(
            status(
                bits=int(StatusBit.STOPPED | StatusBit.FINISHED),
                state=SlotState.COMPLETE,
                voltage=3.55,
                discharge_mah=1520.0,
                discharge_mwh=5480.0,
                discharge_s=1830.0,
            )
        )
    )

    assert len(done) == 1
    cycle = done[0]
    assert cycle["direction"] == "discharge"
    assert cycle["outcome"] == "complete"
    assert cycle["mah"] == 1520.0
    assert cycle["mwh"] == 5480.0
    assert cycle["seconds"] == 1830.0
    # The voltage under load, and the rested one after it, are different
    # facts and both are kept.
    assert cycle["voltage_end_v"] == 3.4
    assert cycle["voltage_rested_v"] == 3.55
    assert cycle["current_max_a"] == 2.5
    assert cycle["slot_number"] == 1


def test_a_pause_does_not_end_a_cycle() -> None:
    """A watchdog pause freezes the counters; a resume continues the run."""
    watcher = CycleWatcher()
    watcher.observe(snap(status(bits=RUNNING_DISCHARGE, discharge_mah=100.0)))
    assert watcher.observe(snap(status(bits=PAUSED_DISCHARGE, discharge_mah=100.0))) == []
    assert watcher.observe(snap(status(bits=PAUSED_DISCHARGE, discharge_mah=100.0))) == []
    assert watcher.observe(snap(status(bits=RUNNING_DISCHARGE, discharge_mah=140.0))) == []

    done = watcher.observe(
        snap(status(bits=int(StatusBit.STOPPED), discharge_mah=300.0))
    )
    assert len(done) == 1
    assert done[0]["mah"] == 300.0
    assert done[0]["outcome"] == "stopped"


def test_charge_then_discharge_is_two_cycles() -> None:
    """A characterisation's rest ends the charge; the discharge is its own."""
    watcher = CycleWatcher()
    watcher.observe(
        snap(status(bits=RUNNING_CHARGE, state=SlotState.CHARGE, charge_mah=2000.0))
    )
    # The rest between the phases: not running, but the test is not over.
    ended = watcher.observe(
        snap(status(bits=0, state=SlotState.REST, charge_mah=2050.0))
    )
    assert len(ended) == 1
    assert ended[0]["direction"] == "charge"
    assert ended[0]["mah"] == 2050.0
    assert ended[0]["outcome"] == "phase"

    watcher.observe(
        snap(
            status(
                bits=RUNNING_DISCHARGE,
                state=SlotState.DISCHARGE,
                charge_mah=2050.0,
                discharge_mah=10.0,
            )
        )
    )
    ended = watcher.observe(
        snap(
            status(
                bits=int(StatusBit.STOPPED | StatusBit.FINISHED),
                state=SlotState.COMPLETE,
                charge_mah=2050.0,
                discharge_mah=1980.0,
            )
        )
    )
    assert len(ended) == 1
    assert ended[0]["direction"] == "discharge"
    assert ended[0]["mah"] == 1980.0


def test_direction_reversal_without_an_idle_sample() -> None:
    """Charge straight into discharge still closes the charge cycle."""
    watcher = CycleWatcher()
    watcher.observe(snap(status(bits=RUNNING_CHARGE, charge_mah=900.0)))
    ended = watcher.observe(
        snap(status(bits=RUNNING_DISCHARGE, charge_mah=900.0, discharge_mah=0.0))
    )
    assert len(ended) == 1
    assert ended[0]["direction"] == "charge"
    assert ended[0]["mah"] == 900.0
    assert ended[0]["outcome"] == "phase"


def test_a_fault_is_reported_as_the_outcome() -> None:
    watcher = CycleWatcher()
    watcher.observe(snap(status(bits=RUNNING_DISCHARGE, discharge_mah=50.0)))
    ended = watcher.observe(
        snap(
            status(
                bits=int(StatusBit.STOPPED),
                state=SlotState.FAULT,
                fault=SlotFault.OVER_TEMP,
                discharge_mah=51.0,
            )
        )
    )
    assert ended[0]["outcome"] == "fault"
    assert ended[0]["fault"] == "over_temp"


def test_counters_zeroed_by_the_next_cycle_fall_back_to_the_peak() -> None:
    """A restarted direction clears the accumulator; the peak is the record."""
    watcher = CycleWatcher()
    watcher.observe(snap(status(bits=RUNNING_DISCHARGE, discharge_mah=1200.0)))
    ended = watcher.observe(
        snap(status(bits=int(StatusBit.STOPPED), discharge_mah=0.0))
    )
    assert ended[0]["mah"] == 1200.0


def test_a_unit_without_bts_counters_falls_back_to_the_proxy() -> None:
    """stats_live=False means the BTS accumulators were never populated.

    The proxy integrates the current itself on such a build. Logging a
    confident-looking 0.0 mAh would be worse than logging that figure, so
    the fallback is taken and labelled.
    """
    watcher = CycleWatcher()
    watcher.observe(
        snap(
            status(bits=RUNNING_DISCHARGE, live_mah=400.0, live_mwh=1400.0),
            stats_live=False,
        )
    )
    ended = watcher.observe(
        snap(
            status(bits=int(StatusBit.STOPPED), live_mah=820.0, live_mwh=2900.0),
            stats_live=False,
        )
    )
    assert ended[0]["mah"] == 820.0
    assert ended[0]["mwh"] == 2900.0
    assert ended[0]["counter_source"] == "proxy"


def test_the_bts_counters_win_when_they_are_live() -> None:
    """The two are measured differently and must not be mixed."""
    watcher = CycleWatcher()
    watcher.observe(snap(status(bits=RUNNING_DISCHARGE, discharge_mah=10.0)))
    ended = watcher.observe(
        snap(status(bits=int(StatusBit.STOPPED), discharge_mah=1900.0, live_mah=12.0))
    )
    assert ended[0]["mah"] == 1900.0
    assert ended[0]["counter_source"] == "bts"


def test_slots_are_independent() -> None:
    watcher = CycleWatcher()
    watcher.observe(
        snap(
            status(0, bits=RUNNING_DISCHARGE, discharge_mah=10.0),
            status(3, bits=RUNNING_CHARGE, charge_mah=20.0),
        )
    )
    ended = watcher.observe(
        snap(
            status(0, bits=int(StatusBit.STOPPED), discharge_mah=11.0),
            status(3, bits=RUNNING_CHARGE, charge_mah=25.0),
        )
    )
    assert [c["slot"] for c in ended] == [0]
    assert ended[0]["mah"] == 11.0


def test_an_idle_slot_never_emits() -> None:
    watcher = CycleWatcher()
    for _ in range(3):
        assert watcher.observe(snap(status(bits=int(StatusBit.STOPPED)))) == []


# --- slot code parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "code,address,slot",
    [
        ("e465b810d93a-3", "E465B810D93A", 2),
        ("E4:65:B8:10:D9:3A S3", "E465B810D93A", 2),
        ("BTS/e465b810d93a/SLOT3", "E465B810D93A", 2),
        ("lion:e465b810d93a:slot-8", "E465B810D93A", 7),
        ("e4-65-b8-10-d9-3a_1", "E465B810D93A", 0),
        # No address: a single-tester bench may label only the slot.
        ("SLOT4", "", 3),
        ("4", "", 3),
    ],
)
def test_parse_slot_code(code: str, address: str, slot: int) -> None:
    assert parse_slot_code(code) == (address, slot)


def test_the_address_digits_are_never_read_as_a_slot() -> None:
    """The address ends in 3A; the slot must come from the trailing 7."""
    assert parse_slot_code("e465b810d93a 7") == ("E465B810D93A", 6)


@pytest.mark.parametrize("code", ["", "   ", "e465b810d93a", "SLOT0", "lion:slot"])
def test_bad_slot_codes_say_what_is_wrong(code: str) -> None:
    with pytest.raises(AssignmentError):
        parse_slot_code(code)


def test_normalise_address_ignores_separators_and_case() -> None:
    assert normalise_address("E4:65:B8:10:D9:3A") == "E465B810D93A"
    assert normalise_address("e4-65-b8-10-d9-3a") == "E465B810D93A"
    assert normalise_address(None) == ""


# --- assignments ------------------------------------------------------------


def test_assign_replaces_and_clear_removes() -> None:
    data = Assignments()
    data.assign(2, "CELL-1", when="t0")
    assert data.code_for(2) == "CELL-1"
    data.assign(2, "CELL-2", when="t1")
    assert data.code_for(2) == "CELL-2"
    assert data.clear(2).code == "CELL-2"
    assert data.code_for(2) == ""
    assert data.clear(2) is None


def test_an_empty_code_is_refused() -> None:
    with pytest.raises(AssignmentError):
        Assignments().assign(0, "   ", when="t0")


def test_relabel_only_touches_uncoded_cycles() -> None:
    """A late scan must not overwrite an attribution that was already right."""
    data = Assignments()
    data.record({"slot": 0, "code": "OLD", "direction": "charge"})
    data.record({"slot": 0, "code": "", "direction": "discharge"})

    changed = data.relabel(0, "LATE")
    assert len(changed) == 1
    assert changed[0]["direction"] == "discharge"
    assert changed[0]["code"] == "LATE"
    assert changed[0]["code_assigned_late"] is True
    # The older, already-attributed cycle is untouched.
    assert data.cycles[1]["code"] == "OLD"


def test_relabel_skips_other_slots() -> None:
    data = Assignments()
    data.record({"slot": 5, "code": ""})
    assert data.relabel(0, "X") == []


def test_the_cycle_tail_is_bounded() -> None:
    data = Assignments()
    for i in range(CYCLE_HISTORY_MAX + 25):
        data.record({"slot": 0, "n": i})
    assert len(data.cycles) == CYCLE_HISTORY_MAX
    # Newest first, oldest dropped.
    assert data.cycles[0]["n"] == CYCLE_HISTORY_MAX + 24
    assert data.cycles[-1]["n"] == 25


def test_last_cycle_is_per_slot() -> None:
    data = Assignments()
    data.record({"slot": 0, "n": 1})
    data.record({"slot": 1, "n": 2})
    data.record({"slot": 0, "n": 3})
    assert data.last_cycle(0)["n"] == 3
    assert data.last_cycle(1)["n"] == 2
    assert data.last_cycle(7) is None


def test_round_trips_through_storage() -> None:
    data = Assignments()
    data.assign(1, "CELL-9", when="t0", slot_code="e465b810d93a-2", note="batch A")
    data.record({"slot": 1, "code": "CELL-9", "mah": 2000.0})

    restored = Assignments.from_json(data.to_json())
    assert restored.code_for(1) == "CELL-9"
    assert restored.get(1).slot_code == "e465b810d93a-2"
    assert restored.get(1).note == "batch A"
    assert restored.cycles == data.cycles


def test_malformed_storage_is_ignored_not_fatal() -> None:
    """A bad store costs a rescan; failing to load would cost the tester."""
    restored = Assignments.from_json(
        {
            "assignments": [
                {"slot": "x", "code": "A"},
                {"code": "no slot"},
                {"slot": 4, "code": "GOOD"},
            ],
            "cycles": ["not a dict", {"slot": 4}],
        }
    )
    assert restored.code_for(4) == "GOOD"
    assert len(restored.slots) == 1
    assert restored.cycles == [{"slot": 4}]

    assert Assignments.from_json(None).slots == {}
    assert Assignments.from_json("nonsense").cycles == []


def test_assignment_as_dict_is_json_safe() -> None:
    assignment = Assignment(slot=0, code="A", assigned_at="t", slot_code="s", note="n")
    assert assignment.as_dict() == {
        "slot": 0,
        "code": "A",
        "assigned_at": "t",
        "slot_code": "s",
        "note": "n",
    }
