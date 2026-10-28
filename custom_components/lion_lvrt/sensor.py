"""Sensor entities: per-slot runtime data and unit-level state.

WHICH MEASUREMENT PATH IS REPORTED
----------------------------------
A slot has two independent measurement paths, and the naming on the wire is
the opposite of what it suggests: ``eChX_CellVoltage`` is the **12-bit
internal ADC**, while the 16-bit ADS131M08 values live in the sense registers.

The BLE slot record carries only the 12-bit path, so that is what a BLE-only
setup reports. Where a register transport is configured, the 16-bit sense
values are exposed as separate entities rather than silently substituted -
an operator comparing a dashboard against a meter needs to know which
converter produced the number.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SlotState, StatusBit
from .coordinator import LionCoordinator
from .entity import LionEntity, LionSlotEntity
from .protocol.ble import SlotStatus, UnitStatus


@dataclass(frozen=True, kw_only=True)
class SlotSensorDescription(SensorEntityDescription):
    """A per-slot sensor and how to pull its value from a status record."""

    value_fn: Callable[[SlotStatus], float | int | str | None]


@dataclass(frozen=True, kw_only=True)
class UnitSensorDescription(SensorEntityDescription):
    value_fn: Callable[[UnitStatus], float | int | str | None]


def _bts_active(status: SlotStatus, charge: float, discharge: float) -> float:
    """The BTS accumulator for the direction the slot is running in.

    The unit keeps one pair per direction, each zeroed only when that
    direction starts, so a slot that charged and then discharged holds both.
    The CHARGING status bit says which one is live; with neither direction
    flagged (idle, ended), the discharge figure is the test's result and is
    the one worth showing.
    """
    if status.status_bits & StatusBit.CHARGING:
        return charge
    if status.status_bits & StatusBit.DISCHARGING:
        return discharge
    return discharge


def _state_name(status: SlotStatus) -> str:
    """Human-readable state, distinguishing *why* a slot is paused.

    A paused slot's reason drives an operator decision, so it belongs in the
    state rather than only in an attribute.
    """
    if status.bts_wd_tripped:
        return "paused_watchdog"
    if status.bts_restored:
        return "paused_restored"
    if status.is_paused:
        return "paused"
    if status.status_bits & StatusBit.CALIBRATING:
        return "calibrating"
    if status.status_bits & StatusBit.SLOT_DISABLED:
        return "disabled"
    return status.state.name.lower()


SLOT_SENSORS: tuple[SlotSensorDescription, ...] = (
    SlotSensorDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.voltage_v,
    ),
    SlotSensorDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        # Negative is discharge, positive is charge.
        value_fn=lambda s: s.current_a,
    ),
    SlotSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.temp_c,
    ),
    SlotSensorDescription(
        key="state",
        translation_key="slot_state",
        device_class=SensorDeviceClass.ENUM,
        options=[
            *(s.name.lower() for s in SlotState),
            "paused",
            "paused_watchdog",
            "paused_restored",
            "calibrating",
            "disabled",
        ],
        value_fn=_state_name,
    ),
    SlotSensorDescription(
        key="fault",
        translation_key="fault",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "none", "over_temp", "under_temp", "over_current", "cmpss_trip",
            "gpio_trip", "timeout", "bts_offline", "unit_state", "no_cell",
            "cell_reversed", "comms",
        ],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.fault.name.lower(),
    ),
    SlotSensorDescription(
        key="live_charge",
        translation_key="live_charge",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        # The BTS's own accumulator for the direction the slot is in. It
        # integrates the ADS131M08 current the loop regulates against, on the
        # C2000's own timebase, and survives a host or link restart - so it is
        # the figure to show. The ESP32's local integration (live_mah) is no
        # longer reported here.
        value_fn=lambda s: _bts_active(s, s.bts_charge_mah, s.bts_discharge_mah),
    ),
    SlotSensorDescription(
        key="live_energy",
        translation_key="live_energy",
        native_unit_of_measurement="mWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: _bts_active(s, s.bts_charge_mwh, s.bts_discharge_mwh),
    ),
    SlotSensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        # May exceed 100% on a cell above its rated capacity - not clamped,
        # because that overshoot is a meaningful result.
        value_fn=lambda s: s.progress * 100.0,
    ),
    SlotSensorDescription(
        key="elapsed",
        translation_key="elapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.elapsed_s,
    ),
    # The BTS's own per-direction accumulators. Each pair is zeroed only when
    # its own direction starts, so a charge followed by a discharge leaves two
    # independent totals - they are TOTAL, not TOTAL_INCREASING.
    SlotSensorDescription(
        key="bts_charge_mah",
        translation_key="bts_charge_mah",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.bts_charge_mah,
    ),
    SlotSensorDescription(
        key="bts_charge_mwh",
        translation_key="bts_charge_mwh",
        native_unit_of_measurement="mWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.bts_charge_mwh,
    ),
    SlotSensorDescription(
        key="bts_discharge_mah",
        translation_key="bts_discharge_mah",
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda s: s.bts_discharge_mah,
    ),
    SlotSensorDescription(
        key="bts_discharge_mwh",
        translation_key="bts_discharge_mwh",
        native_unit_of_measurement="mWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda s: s.bts_discharge_mwh,
    ),
    SlotSensorDescription(
        key="bts_charge_seconds",
        translation_key="bts_charge_seconds",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.bts_charge_seconds,
    ),
    SlotSensorDescription(
        key="bts_discharge_seconds",
        translation_key="bts_discharge_seconds",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.bts_discharge_seconds,
    ),
)


#: ``eSlotMode`` low two bits: slots per control group.
_GROUPING = {0: "independent", 1: "pairs", 2: "quads", 3: "octet"}


def _mode_strap_name(unit: UnitStatus) -> str | None:
    """The MODE dip strap in words, or None on firmware that does not report it.

    The low two bits are the group size; bit 2 moves the voltage feedback from
    the ADS131M08 to the C2000's internal ADC.
    """
    mode = unit.slot_mode
    if mode is None:
        return None
    name = _GROUPING[mode & 3]
    return f"{name} (internal ADC)" if mode & 4 else name


UNIT_SENSORS: tuple[UnitSensorDescription, ...] = (
    # How the unit is strapped. First in the list because everything else on
    # the page - which slots exist, which accept commands - follows from it.
    UnitSensorDescription(
        key="mode_strap",
        translation_key="mode_strap",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_mode_strap_name,
    ),
    UnitSensorDescription(
        key="slots_enabled",
        translation_key="slots_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The ENABLE strap gives the highest enabled slot, not a mask, so this
        # is a count and slots above it are masked off.
        value_fn=lambda u: u.slots_enabled,
    ),
    UnitSensorDescription(
        key="input_voltage",
        translation_key="input_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda u: u.input_voltage_v,
    ),
    UnitSensorDescription(
        key="unit_state",
        translation_key="unit_state",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "input_low_charge_disabled",
            "input_low_charge_restricted",
            "input_ok",
            "input_high_discharge_restricted",
            "input_high_discharge_disabled",
        ],
        value_fn=lambda u: u.unit_state.name.lower(),
    ),
    UnitSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.uptime_s,
    ),
    UnitSensorDescription(
        key="watchdog_timeout",
        translation_key="watchdog_timeout",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        # 0 means supervision is off entirely - the converters will keep
        # running if this host goes away mid-test.
        value_fn=lambda u: u.watchdog_timeout_s,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LionCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        LionUnitSensor(coordinator, description) for description in UNIT_SENSORS
    ]
    for slot in range(coordinator.device.slot_count):
        entities.extend(
            LionSlotSensor(coordinator, slot, description)
            for description in SLOT_SENSORS
        )
        entities.append(LionSlotCodeSensor(coordinator, slot))
    async_add_entities(entities)


class LionSlotSensor(LionSlotEntity, SensorEntity):
    """One measurement of one slot."""

    entity_description: SlotSensorDescription

    def __init__(
        self,
        coordinator: LionCoordinator,
        slot: int,
        description: SlotSensorDescription,
    ) -> None:
        super().__init__(coordinator, slot, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        status = self.status
        if status is None:
            return None
        return self.entity_description.value_fn(status)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the raw status word on the state sensor.

        Decoding it by hand in a template is error-prone - the same bit names
        mean positions in one firmware header and masks in the other - so the
        decoded flags are published alongside the raw value.
        """
        if self.entity_description.key != "state":
            return None
        status = self.status
        if status is None:
            return None
        bits = status.status_bits
        return {
            "status_bits": bits,
            "status_hex": f"0x{bits:06X}",
            "flags": sorted(flag.name.lower() for flag in StatusBit if bits & flag),
            "resume_direction": status.resume_direction,
            "needs_operator": status.needs_operator,
            "calibration_voltage_valid": bool(bits & StatusBit.CAL_V_VALID),
            "calibration_current_valid": bool(bits & StatusBit.CAL_I_VALID),
            "group_follower": bool(bits & StatusBit.SLAVE_MODE),
        }


class LionUnitSensor(LionEntity, SensorEntity):
    """One measurement of the unit."""

    entity_description: UnitSensorDescription

    def __init__(
        self, coordinator: LionCoordinator, description: UnitSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data.unit)


class LionSlotCodeSensor(LionSlotEntity, SensorEntity):
    """The cell code a scan bound to this slot.

    Not a measurement of the tester - it is Home Assistant's own record of
    which cell is in the holder, and the thing a finished cycle is filed
    under. It survives a restart and a link outage, so it stays available
    even when the slot's other sensors do not.
    """

    _attr_translation_key = "assigned_code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "assigned_code")

    @property
    def available(self) -> bool:
        # Deliberately not LionSlotEntity.available: the assignment is local
        # and remains true and useful while the tester is unreachable.
        return True

    @property
    def native_value(self) -> str | None:
        return self.coordinator.assignments.data.code_for(self.slot) or None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.assignments.data
        assignment = data.get(self.slot)
        last = data.last_cycle(self.slot)
        attributes: dict[str, object] = {
            "assigned_at": assignment.assigned_at if assignment else None,
            "slot_code": assignment.slot_code if assignment else None,
            "note": assignment.note if assignment else None,
        }
        if last is not None:
            attributes.update(
                last_cycle_direction=last.get("direction"),
                last_cycle_outcome=last.get("outcome"),
                last_cycle_mah=last.get("mah"),
                last_cycle_mwh=last.get("mwh"),
                last_cycle_seconds=last.get("seconds"),
                last_cycle_code=last.get("code"),
                last_cycle_at=last.get("timestamp"),
            )
        return attributes
