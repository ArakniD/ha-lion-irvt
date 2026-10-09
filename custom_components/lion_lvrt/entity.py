"""Shared entity base classes.

Two device registry entries per unit: one for the tester itself and one per
slot, so a slot's sensors, its mode control and its diagnostics group together
in the UI instead of forming a flat list of 8 x N entities.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LionCoordinator
from .protocol.ble import SlotStatus

#: ``via_device`` is deprecated in favour of ``via_device_id`` and stops working
#: in Home Assistant 2027.8.0. ``hacs.json`` still declares an older minimum
#: version, so use the new key only where this Home Assistant knows it.
_HAS_VIA_DEVICE_ID = "via_device_id" in getattr(DeviceInfo, "__annotations__", {})


class LionEntity(CoordinatorEntity[LionCoordinator]):
    """Anything attached to the unit as a whole."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LionCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=self.coordinator.entry.title,
            manufacturer="Lion IRVT",
            model="TIDA-010086 8-channel battery tester",
            sw_version=(
                f"BLE proto {self.coordinator.data.unit.version}"
                if self.coordinator.data
                else None
            ),
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.unit.online
        )


class LionSlotEntity(CoordinatorEntity[LionCoordinator]):
    """Anything attached to one slot."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LionCoordinator, slot: int, key: str) -> None:
        super().__init__(coordinator)
        self._slot = slot
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_slot{slot}_{key}"

    @property
    def slot(self) -> int:
        return self._slot

    @property
    def status(self) -> SlotStatus | None:
        """This slot's last known status, or None before the first poll."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.slots.get(self._slot)

    @property
    def device_info(self) -> DeviceInfo:
        entry_id = self.coordinator.entry.entry_id
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_slot{self._slot}")},
            # Front-panel slots are 1-based; the wire is 0-based. Naming the
            # entity the way the panel is labelled avoids an off-by-one every
            # time an operator maps a dashboard to the hardware.
            name=f"Slot {self._slot + 1}",
            manufacturer="Lion IRVT",
            model="Battery test slot",
        )
        # The parent is created in async_setup_entry before any platform is
        # set up, so the lookup finds it. If it somehow does not, the slot
        # still registers - it just is not nested under the tester.
        parent = None
        if _HAS_VIA_DEVICE_ID:
            parent = dr.async_get(self.coordinator.hass).async_get_device(
                identifiers={(DOMAIN, entry_id)}
            )
        if parent is not None:
            info["via_device_id"] = parent.id
        elif not _HAS_VIA_DEVICE_ID:
            info["via_device"] = (DOMAIN, entry_id)
        return info

    @property
    def available(self) -> bool:
        if not super().available or self.coordinator.data is None:
            return False
        if not self.coordinator.data.unit.online:
            return False
        status = self.status
        if status is None:
            return False
        # A strap-disabled slot has no hardware behind it; reporting its
        # sensors as 0.0 would be indistinguishable from a real measurement.
        from .const import StatusBit  # noqa: PLC0415

        return not status.status_bits & StatusBit.SLOT_DISABLED
