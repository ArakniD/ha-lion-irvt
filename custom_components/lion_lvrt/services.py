"""Services for operations that do not fit an entity.

Four kinds live here:

* **Slot configuration** - a cell profile is eight coupled fields written as
  one record, which is a service call rather than eight number entities that
  could be left half-applied.
* **Forced resume** - the override for a slot the unit paused on a watchdog
  trip or an F-RAM restore. Deliberately not a button: it needs an explicit
  argument so it cannot be pressed by accident.
* **Calibration** - a stateful bench procedure driven opcode by opcode.
* **Barcode assignment** - binding a scanned cell code to a slot, which is
  what makes a finished cycle attributable to a particular cell.

THE SCANNING SERVICE IS ADDRESSED BY BLUETOOTH ADDRESS, NOT BY ENTRY ID
-----------------------------------------------------------------------
``assign_code`` takes the tester's Bluetooth address because that is what a
phone dashboard can put in a barcode. An ``entry_id`` is a Home Assistant
internal that nobody can print on a label, and a bench with two testers on it
needs the call to be unambiguous. The address may be given on its own or
carried inside the scanned slot code.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .assignments import AssignmentError, parse_slot_code
from .const import (
    CAL_FIXED_CURRENT_MAX_PU,
    CalCommand,
    CellChemistry,
    DOMAIN,
    EVENT_CODE_ASSIGNED,
    SLOT_COUNT,
)
from .coordinator import LionCoordinator
from .cycles import normalise_address
from .protocol.ble import SlotConfig
from .transport.base import TransportError, UnitRefusedError

_LOGGER = logging.getLogger(__name__)

SERVICE_CONFIGURE_SLOT = "configure_slot"
SERVICE_SET_SERIAL = "set_serial"
SERVICE_RESUME_SLOT = "resume_slot"
SERVICE_SET_MODE = "set_mode"
SERVICE_CALIBRATION = "calibration_command"
SERVICE_GET_RESULT = "get_result"
SERVICE_SYNC_MODELS = "sync_models"
SERVICE_DELETE_MODEL = "delete_model"
SERVICE_ASSIGN_CODE = "assign_code"
SERVICE_CLEAR_CODE = "clear_code"
SERVICE_GET_CYCLES = "get_cycles"

ATTR_ENTRY = "entry_id"
ATTR_SLOT = "slot"

_SLOT = vol.All(vol.Coerce(int), vol.Range(min=0, max=SLOT_COUNT - 1))

_BASE = {
    vol.Required(ATTR_SLOT): _SLOT,
    vol.Optional(ATTR_ENTRY): cv.string,
}

CONFIGURE_SCHEMA = vol.Schema(
    {
        **_BASE,
        vol.Required("chemistry"): vol.In([c.name.lower() for c in CellChemistry]),
        vol.Optional("model_name", default=""): cv.string,
        vol.Optional("serial", default=""): cv.string,
        # 0 means "use the model default" for each of these, which is why the
        # minimum is 0 rather than something positive.
        vol.Optional("capacity_mah", default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100000)
        ),
        vol.Optional("charge_c", default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=10)
        ),
        vol.Optional("discharge_c", default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=10)
        ),
        vol.Optional("auto_recharge", default=False): cv.boolean,
        vol.Optional("shipping_pct", default=60.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional("max_duration_minutes", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100000)
        ),
    }
)

SET_SERIAL_SCHEMA = vol.Schema(
    {**_BASE, vol.Required("serial"): vol.All(cv.string, vol.Length(min=1, max=31))}
)

RESUME_SCHEMA = vol.Schema({**_BASE, vol.Optional("force", default=False): cv.boolean})

SET_MODE_SCHEMA = vol.Schema(
    {**_BASE, vol.Required("mode"): vol.In(["stop", "charge", "discharge"])}
)

CALIBRATION_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Required("command"): vol.In([c.name.lower() for c in CalCommand if c]),
        vol.Optional(ATTR_SLOT): _SLOT,
        vol.Optional("value", default=0.0): vol.Coerce(float),
    }
)

GET_RESULT_SCHEMA = vol.Schema(_BASE)


SYNC_MODELS_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY): cv.string})
DELETE_MODEL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=7)),
    }
)

#
# The scanning services.
#
# `slot_code` and `slot` are alternatives, not both: a dashboard that scans a
# slot label sends the code, and one with a slot picker sends the number.
# `address` is optional in either case - a slot code usually carries it, and
# a single configured tester needs no addressing at all.
#
ASSIGN_CODE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional("address"): cv.string,
        vol.Optional("slot_code"): cv.string,
        vol.Optional(ATTR_SLOT): _SLOT,
        vol.Required("code"): vol.All(cv.string, vol.Length(min=1, max=64)),
        vol.Optional("note", default=""): cv.string,
        # The operator who scans the cell after the run rather than before.
        # Off by default: silently relabelling finished work is not something
        # to do without being asked.
        vol.Optional("apply_to_last_cycle", default=False): cv.boolean,
        # Also push the code to the tester as the slot's serial, so it lands
        # in the unit's own stored result.
        vol.Optional("set_serial", default=True): cv.boolean,
    }
)

CLEAR_CODE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional("address"): cv.string,
        vol.Optional("slot_code"): cv.string,
        vol.Optional(ATTR_SLOT): _SLOT,
    }
)

GET_CYCLES_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional("address"): cv.string,
        vol.Optional(ATTR_SLOT): _SLOT,
        vol.Optional("code"): cv.string,
        vol.Optional("limit", default=20): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=200)
        ),
    }
)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> LionCoordinator:
    """Resolve which unit a call is addressed to.

    With a single unit configured the ``entry_id`` may be omitted; with more
    than one it is required, because guessing would command the wrong
    hardware.
    """
    entries: dict[str, LionCoordinator] = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("no Lion LVRT tester is configured")

    entry_id = call.data.get(ATTR_ENTRY)
    if entry_id is not None:
        if entry_id not in entries:
            raise HomeAssistantError(f"no configured tester with entry_id {entry_id}")
        return entries[entry_id]

    if len(entries) > 1:
        raise HomeAssistantError(
            f"{len(entries)} testers are configured; pass entry_id to say "
            "which one this call is for"
        )
    return next(iter(entries.values()))


def _by_address(hass: HomeAssistant, address: str) -> LionCoordinator:
    """Find the tester whose Bluetooth address matches ``address``.

    Separators and case are ignored, so ``E4:65:B8:10:D9:3A`` and a barcode's
    ``e465b810d93a`` resolve to the same unit.
    """
    entries: dict[str, LionCoordinator] = hass.data.get(DOMAIN, {})
    wanted = normalise_address(address)
    matches = [c for c in entries.values() if c.address == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = ", ".join(sorted(c.address for c in entries.values() if c.address))
        raise HomeAssistantError(
            f"no configured tester has the address {address!r}"
            + (f" (configured: {known})" if known else "")
        )
    raise HomeAssistantError(
        f"{len(matches)} configured testers share the address {address!r}; "
        "pass entry_id instead"
    )


def _target(hass: HomeAssistant, call: ServiceCall) -> tuple[LionCoordinator, int]:
    """Resolve a scanning call to one tester and one 0-based slot.

    The address and slot may arrive three ways - inside a scanned slot code,
    as explicit fields, or implied by there being a single tester. An
    explicit field always wins over the scanned code, so a dashboard can
    correct a mislabelled socket without re-printing the label.
    """
    address = call.data.get("address") or ""
    slot = call.data.get(ATTR_SLOT)
    slot_code = call.data.get("slot_code") or ""

    if slot_code:
        try:
            scanned_address, scanned_slot = parse_slot_code(slot_code)
        except AssignmentError as err:
            raise HomeAssistantError(str(err)) from err
        if not address:
            address = scanned_address
        if slot is None:
            slot = scanned_slot

    if slot is None:
        raise HomeAssistantError(
            "no slot was given: pass 'slot', or a 'slot_code' that carries "
            "the slot number"
        )

    if call.data.get(ATTR_ENTRY):
        coordinator = _coordinator(hass, call)
    elif address:
        coordinator = _by_address(hass, address)
    else:
        coordinator = _coordinator(hass, call)

    if not 0 <= slot < coordinator.device.slot_count:
        raise HomeAssistantError(
            f"slot {slot + 1} does not exist on this tester "
            f"(1-{coordinator.device.slot_count})"
        )
    return coordinator, slot


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CONFIGURE_SLOT):
        return

    async def configure_slot(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        slot = call.data[ATTR_SLOT]
        config = SlotConfig(
            slot=slot,
            chemistry=CellChemistry[call.data["chemistry"].upper()],
            auto_recharge=call.data["auto_recharge"],
            capacity_mah=call.data["capacity_mah"],
            charge_c=call.data["charge_c"],
            discharge_c=call.data["discharge_c"],
            shipping_pct=call.data["shipping_pct"],
            max_duration_minutes=call.data["max_duration_minutes"],
            model_name=call.data["model_name"],
            serial=call.data["serial"],
        )
        await _run(coordinator.device.async_write_config(config))
        await coordinator.async_request_refresh()

    async def set_serial(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        await _run(
            coordinator.device.async_set_serial(
                call.data[ATTR_SLOT], call.data["serial"]
            )
        )

    async def resume_slot(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        await _run(
            coordinator.device.async_resume(
                call.data[ATTR_SLOT], force=call.data["force"]
            )
        )
        await coordinator.async_request_refresh()

    async def set_mode(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        device = coordinator.device
        slot = call.data[ATTR_SLOT]
        mode = call.data["mode"]
        if mode == "charge":
            await _run(device.async_charge(slot))
        elif mode == "discharge":
            await _run(device.async_discharge(slot))
        else:
            await _run(device.async_stop(slot))
        await coordinator.async_request_refresh()

    async def calibration_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        opcode = CalCommand[call.data["command"].upper()]
        slot = call.data.get(ATTR_SLOT, 0)
        value = call.data["value"]

        if opcode is CalCommand.ENTER and ATTR_SLOT not in call.data:
            raise HomeAssistantError("the 'enter' command needs a slot")
        if opcode is CalCommand.SET_FIXED_CURRENT and not (
            0.0 <= value <= CAL_FIXED_CURRENT_MAX_PU
        ):
            raise HomeAssistantError(
                f"fixed current must be 0.0-{CAL_FIXED_CURRENT_MAX_PU} pu "
                "(1.0 pu is about 10 A)"
            )

        try:
            status = await coordinator.device.async_cal_command(opcode, slot, value)
        except (UnitRefusedError, TransportError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

        # The live per-unit readings are what an operator adjusts a bench
        # supply against, so they are returned rather than only logged.
        return {
            "slot": status.slot,
            "active": status.active,
            "status_bits": status.status_bits,
            "result": status.result.name.lower(),
            "voltage_valid": status.v_tick,
            "current_valid": status.i_tick,
            "ads_v_pu": status.ads_v_pu,
            "ads_i_pu": status.ads_i_pu,
            "ads_v_v": status.ads_v_v,
            "ads_i_a": status.ads_i_a,
            "f28_v_pu": status.f28_v_pu,
            "f28_i_pu": status.f28_i_pu,
            "temp_c": status.temp_c,
        }

    async def get_result(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        slot = call.data[ATTR_SLOT]
        try:
            result = await coordinator.device.async_read_result(slot)
        except (TransportError, UnitRefusedError) as err:
            raise HomeAssistantError(str(err)) from err
        return {
            "slot": result.slot,
            "valid": result.valid,
            "serial": result.serial,
            "model_name": result.model_name,
            "chemistry": result.chemistry.name.lower(),
            "discharge_mah": result.discharge_mah,
            "discharge_mwh": result.discharge_mwh,
            "discharge_seconds": result.discharge_seconds,
            "start_voltage_v": result.start_voltage_v,
            "end_voltage_v": result.end_voltage_v,
            "rested_voltage_v": result.rested_voltage_v,
            "max_temp_c": result.max_temp_c,
            "max_current_a": result.max_current_a,
            "recharged": result.recharged,
            "recharge_mah": result.recharge_mah,
            # Seconds of proxy uptime, NOT a wall clock - the device has no
            # RTC. A caller that needs a timestamp applies its own now.
            "completed_uptime_s": result.completed_uptime_s,
        }

    async def sync_models(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        return await async_sync_models_file(hass, coordinator)

    async def delete_model(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        await _run(coordinator.device.async_delete_model(call.data["name"]))
        coordinator.async_update_listeners()

    async def assign_code(call: ServiceCall) -> ServiceResponse:
        coordinator, slot = _target(hass, call)
        code = call.data["code"].strip()
        store = coordinator.assignments

        try:
            assignment = store.data.assign(
                slot,
                code,
                when=dt_util.utcnow().isoformat(),
                slot_code=call.data.get("slot_code", ""),
                note=call.data["note"],
            )
        except AssignmentError as err:
            raise HomeAssistantError(str(err)) from err

        relabelled: list[dict] = []
        if call.data["apply_to_last_cycle"]:
            relabelled = store.data.relabel(slot, code)
        store.save()

        # Give the code to the tester as well, so it reaches the unit's own
        # stored result and the front panel. Best-effort: a link that is down
        # must not lose the assignment Home Assistant just recorded, and the
        # serial can be pushed again later.
        serial_error = ""
        if call.data["set_serial"]:
            try:
                await coordinator.device.async_set_serial(slot, code[:31])
            except (TransportError, UnitRefusedError, ValueError) as err:
                serial_error = str(err)
                _LOGGER.warning(
                    "Slot %d is assigned to %s here, but the tester would not "
                    "take it as a serial: %s",
                    slot + 1,
                    code,
                    err,
                )

        hass.bus.async_fire(
            EVENT_CODE_ASSIGNED,
            {
                "entry_id": coordinator.entry.entry_id,
                "tester": coordinator.entry.title,
                "address": coordinator.address,
                "slot": slot,
                "slot_number": slot + 1,
                "code": code,
                "action": "assigned",
                "timestamp": assignment.assigned_at,
            },
        )
        coordinator.async_update_listeners()

        return {
            "slot": slot,
            "slot_number": slot + 1,
            "code": code,
            "tester": coordinator.entry.title,
            "address": coordinator.address,
            "assigned_at": assignment.assigned_at,
            "relabelled_cycles": relabelled,
            "serial_written": call.data["set_serial"] and not serial_error,
            "serial_error": serial_error,
        }

    async def clear_code(call: ServiceCall) -> None:
        coordinator, slot = _target(hass, call)
        previous = coordinator.assignments.data.clear(slot)
        coordinator.assignments.save()
        hass.bus.async_fire(
            EVENT_CODE_ASSIGNED,
            {
                "entry_id": coordinator.entry.entry_id,
                "tester": coordinator.entry.title,
                "address": coordinator.address,
                "slot": slot,
                "slot_number": slot + 1,
                "code": previous.code if previous else "",
                "action": "cleared",
                "timestamp": dt_util.utcnow().isoformat(),
            },
        )
        coordinator.async_update_listeners()

    async def get_cycles(call: ServiceCall) -> ServiceResponse:
        """Return the recent cycle tail this integration kept.

        A convenience for a dashboard. The full history lives in Home
        Assistant's recorder under the ``lion_lvrt_cycle_completed`` event -
        this is a bounded tail, not an archive.
        """
        address = call.data.get("address")
        if call.data.get(ATTR_ENTRY) or not address:
            coordinator = _coordinator(hass, call)
        else:
            coordinator = _by_address(hass, address)

        slot = call.data.get(ATTR_SLOT)
        code = (call.data.get("code") or "").strip().upper()
        cycles = [
            cycle
            for cycle in coordinator.assignments.data.cycles
            if (slot is None or cycle.get("slot") == slot)
            and (not code or str(cycle.get("code", "")).upper() == code)
        ]
        return {
            "tester": coordinator.entry.title,
            "address": coordinator.address,
            "count": len(cycles[: call.data["limit"]]),
            "cycles": cycles[: call.data["limit"]],
        }

    hass.services.async_register(
        DOMAIN, SERVICE_CONFIGURE_SLOT, configure_slot, schema=CONFIGURE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_MODELS,
        sync_models,
        schema=SYNC_MODELS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_MODEL, delete_model, schema=DELETE_MODEL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SERIAL, set_serial, schema=SET_SERIAL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_SLOT, resume_slot, schema=RESUME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MODE, set_mode, schema=SET_MODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATION,
        calibration_command,
        schema=CALIBRATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RESULT,
        get_result,
        schema=GET_RESULT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASSIGN_CODE,
        assign_code,
        schema=ASSIGN_CODE_SCHEMA,
        # OPTIONAL rather than ONLY: a dashboard button fires it without
        # caring about the reply, and a script that wants the confirmation
        # asks for it.
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_CODE, clear_code, schema=CLEAR_CODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CYCLES,
        get_cycles,
        schema=GET_CYCLES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _run(awaitable) -> None:
    """Translate the device layer's errors into Home Assistant's."""
    try:
        await awaitable
    except (UnitRefusedError, TransportError, ValueError) as err:
        raise HomeAssistantError(str(err)) from err


def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_CONFIGURE_SLOT,
        SERVICE_SET_SERIAL,
        SERVICE_RESUME_SLOT,
        SERVICE_SET_MODE,
        SERVICE_CALIBRATION,
        SERVICE_GET_RESULT,
        SERVICE_SYNC_MODELS,
        SERVICE_DELETE_MODEL,
        SERVICE_ASSIGN_CODE,
        SERVICE_CLEAR_CODE,
        SERVICE_GET_CYCLES,
    ):
        hass.services.async_remove(DOMAIN, service)


async def async_sync_models_file(
    hass: HomeAssistant, coordinator: LionCoordinator
) -> dict:
    """Upload the models file to the tester and report what happened.

    Used by the sync_models service and once at start-up. Problems are logged
    and returned rather than raised, so one bad entry does not stop the rest.
    """
    from .models_file import (  # noqa: PLC0415
        MODELS_FILENAME,
        ModelFileError,
        load_models_file,
    )

    try:
        models, problems = await hass.async_add_executor_job(
            load_models_file, hass.config.config_dir
        )
    except ModelFileError as err:
        raise HomeAssistantError(str(err)) from err

    uploaded = 0
    if models:
        try:
            uploaded, refused = await coordinator.device.async_sync_models(models)
        except (TransportError, UnitRefusedError) as err:
            raise HomeAssistantError(str(err)) from err
        problems.extend(refused)
    for problem in problems:
        _LOGGER.warning("%s: %s", MODELS_FILENAME, problem)
    coordinator.async_update_listeners()
    return {
        "file": MODELS_FILENAME,
        "models_in_file": len(models),
        "uploaded": uploaded,
        "problems": problems,
    }
