"""Per-slot cell profile: chemistry, model, capacity and C-rates.

These mirror the slot's configuration on the proxy (the BLE 0005 record, or
``/api/slot/<n>``), which the proxy now keeps across a reboot. Each entity
shows what the device holds and writes a change back as a whole config, so
the other fields stay as they were.

The model list is the proxy's own catalogue - the built-ins plus any custom
models uploaded from the models file - so a dropdown only ever offers a name
the tester will accept.

The configuration is read on demand and cached on the device object: it is
not part of the 1 Hz poll. A read happens when the entities are added, when
the link comes back, and at a slow interval after that.
"""

from __future__ import annotations

import dataclasses
import logging
import time

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import CellChemistry
from .coordinator import LionCoordinator
from .entity import LionSlotEntity
from .protocol.ble import SlotConfig
from .transport.base import TransportError, UnitRefusedError

_LOGGER = logging.getLogger(__name__)

#: Shown in the model dropdown for "no model: chemistry and capacity only".
MODEL_NONE = "none"

_REFRESH_S = 60.0


class _ProfileEntity(LionSlotEntity):
    """Shared read/cache/write behaviour for one slot's configuration."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: LionCoordinator, slot: int, key: str) -> None:
        super().__init__(coordinator, slot, key)
        self._last_read = 0.0
        self._reading = False

    @property
    def config(self) -> SlotConfig | None:
        return self.coordinator.device.cached_config(self.slot)

    async def _async_read(self) -> None:
        if self._reading:
            return
        self._reading = True
        try:
            await self.coordinator.device.async_read_config(self.slot)
            self._last_read = time.monotonic()
        except (TransportError, UnitRefusedError) as err:
            _LOGGER.debug("Slot %d config not read: %s", self.slot, err)
        finally:
            self._reading = False

    async def _async_read_and_write_state(self) -> None:
        await self._async_read()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.config is None:
            self.hass.async_create_task(self._async_read_and_write_state())

    def _handle_coordinator_update(self) -> None:
        stale = (time.monotonic() - self._last_read) >= _REFRESH_S
        cached = self.config
        if (cached is None or stale) and self.available and not self._reading:
            self.hass.async_create_task(self._async_read_and_write_state())
        super()._handle_coordinator_update()

    async def _async_write(self, **changes) -> None:
        current = self.config
        if current is None:
            await self._async_read()
            current = self.config
        if current is None:
            raise HomeAssistantError(
                f"slot {self.slot + 1}'s configuration could not be read, so it "
                "cannot be changed field by field. Use the configure_slot service"
            )
        updated = dataclasses.replace(current, slot=self.slot, **changes)
        try:
            await self.coordinator.device.async_write_config(updated)
        except (TransportError, UnitRefusedError, ValueError) as err:
            # A running slot refuses reconfiguration; say so plainly.
            raise HomeAssistantError(
                f"slot {self.slot + 1} refused the change ({err}). A running "
                "slot cannot be reconfigured"
            ) from err
        self._last_read = time.monotonic()
        # Every profile entity for this slot shares the cache - refresh them all.
        self.coordinator.async_update_listeners()


class LionSlotChemistrySelect(_ProfileEntity, SelectEntity):
    _attr_translation_key = "chemistry"

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "chemistry")

    @property
    def options(self) -> list[str]:
        return [c.name.lower() for c in CellChemistry]

    @property
    def current_option(self) -> str | None:
        cfg = self.config
        return cfg.chemistry.name.lower() if cfg else None

    async def async_select_option(self, option: str) -> None:
        chemistry = CellChemistry[option.upper()]
        # A model brings its own chemistry; choosing a different chemistry
        # means the model no longer applies.
        changes: dict = {"chemistry": chemistry}
        cfg = self.config
        if cfg is not None and cfg.model_name:
            model = _find_model(self.coordinator, cfg.model_name)
            if model is None or model.chemistry != chemistry:
                changes["model_name"] = ""
                if not cfg.capacity_mah and model is not None:
                    # Keep the capacity the model was supplying: a bare
                    # chemistry has none and would not resolve.
                    changes["capacity_mah"] = model.capacity_mah
        await self._async_write(**changes)


class LionSlotModelSelect(_ProfileEntity, SelectEntity):
    _attr_translation_key = "model"

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "model")

    @property
    def options(self) -> list[str]:
        names = [m.name for m in self.coordinator.device.models]
        options = [MODEL_NONE, *names]
        # The slot's model must be listed even before the catalogue has been
        # read, or Home Assistant shows the entity as invalid.
        current = self.current_option
        if current and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        cfg = self.config
        if cfg is None:
            return None
        return cfg.model_name or MODEL_NONE

    @property
    def extra_state_attributes(self) -> dict:
        model = _find_model(self.coordinator, self.config.model_name if self.config else "")
        if model is None:
            return {}
        return {
            "manufacturer": model.manufacturer,
            "chemistry": model.chemistry.name.lower(),
            "rated_capacity_mah": model.capacity_mah,
            "charge_current_max_a": model.charge_current_max_a,
            "discharge_current_max_a": model.discharge_current_max_a,
            "custom": model.custom,
        }

    async def async_select_option(self, option: str) -> None:
        if option == MODEL_NONE:
            cfg = self.config
            changes: dict = {"model_name": ""}
            if cfg is not None and cfg.model_name and not cfg.capacity_mah:
                model = _find_model(self.coordinator, cfg.model_name)
                changes["capacity_mah"] = model.capacity_mah if model else 3000.0
            await self._async_write(**changes)
            return
        model = _find_model(self.coordinator, option)
        if model is None:
            raise HomeAssistantError(f"the tester has no model named {option}")
        # The model's own capacity applies unless one was set by hand.
        await self._async_write(
            model_name=model.name, chemistry=model.chemistry, capacity_mah=0.0
        )


class _ProfileNumber(_ProfileEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _field: str

    @property
    def native_value(self) -> float | None:
        cfg = self.config
        return float(getattr(cfg, self._field)) if cfg else None

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(**{self._field: float(value)})


class LionSlotCapacityNumber(_ProfileNumber):
    """Rated capacity. 0 = the model's datasheet capacity."""

    _attr_translation_key = "capacity"
    _attr_native_unit_of_measurement = "mAh"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100000.0
    _attr_native_step = 1.0
    _field = "capacity_mah"

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "capacity")


class LionSlotChargeCNumber(_ProfileNumber):
    """Charge C-rate. 0 = the chemistry default."""

    _attr_translation_key = "charge_c"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.05
    _field = "charge_c"

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "charge_c")


class LionSlotDischargeCNumber(_ProfileNumber):
    """Discharge C-rate. 0 = the chemistry default."""

    _attr_translation_key = "discharge_c"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.05
    _field = "discharge_c"

    def __init__(self, coordinator: LionCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "discharge_c")


def _find_model(coordinator: LionCoordinator, name: str):
    if not name:
        return None
    for model in coordinator.device.models:
        if model.name.upper() == name.upper():
            return model
    return None


def profile_selects(coordinator: LionCoordinator) -> list[SelectEntity]:
    if not coordinator.device.supports_test_engine:
        return []
    out: list[SelectEntity] = []
    for slot in range(coordinator.device.slot_count):
        out.append(LionSlotChemistrySelect(coordinator, slot))
        out.append(LionSlotModelSelect(coordinator, slot))
    return out


def profile_numbers(coordinator: LionCoordinator) -> list[NumberEntity]:
    if not coordinator.device.supports_test_engine:
        return []
    out: list[NumberEntity] = []
    for slot in range(coordinator.device.slot_count):
        out.append(LionSlotCapacityNumber(coordinator, slot))
        out.append(LionSlotChargeCNumber(coordinator, slot))
        out.append(LionSlotDischargeCNumber(coordinator, slot))
    return out
