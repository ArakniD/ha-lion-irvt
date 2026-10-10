"""The Lion LVRT battery tester integration.

Set-up builds a :class:`LionDevice` from the configured transports, wires it
to a coordinator, and registers the services. The BLE path goes through Home
Assistant's own Bluetooth stack, so an ESPHome or Shelly Bluetooth proxy is
used automatically when it has the better signal - nothing here needs to know
whether the adapter is local or remote.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_ADDRESS,
    CONF_CAN_BITRATE,
    CONF_CAN_INTERFACE,
    CONF_HTTP_HOST,
    CONF_SLOT_COUNT,
    CONF_TRANSPORT,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    SLOT_COUNT,
    TRANSPORT_BLE,
    TRANSPORT_CAN,
    TRANSPORT_HTTP,
)
from .coordinator import LionCoordinator
from .device import LionDevice
from .services import async_register_services, async_unregister_services
from .transport.base import Transport, TransportError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    slot_count = entry.data.get(CONF_SLOT_COUNT, SLOT_COUNT)

    try:
        primary = await _build_primary(hass, entry, slot_count)
        secondary = await _build_register_transport(hass, entry, slot_count)
    except TransportError as err:
        raise ConfigEntryNotReady(str(err)) from err

    device = LionDevice(
        primary, register_transport=secondary, slot_count=slot_count
    )

    try:
        await device.async_connect()
    except TransportError as err:
        raise ConfigEntryNotReady(f"could not reach the tester: {err}") from err

    coordinator = LionCoordinator(
        hass, device, entry, scan_interval_s=DEFAULT_SCAN_INTERVAL_S
    )
    await coordinator.async_config_entry_first_refresh()

    # Subscribe for the low-latency path. The poll continues underneath:
    # notifications are best-effort and are dropped without retry when the
    # device's buffer pool is exhausted.
    start_notify = getattr(primary, "async_start_notify", None)
    if start_notify is not None and primary.supports_notifications:
        try:
            await start_notify(coordinator.handle_push)
        except TransportError as err:
            _LOGGER.warning(
                "Could not subscribe to notifications, falling back to "
                "polling only: %s",
                err,
            )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Create the tester's own device first. The slot devices point at it with
    # via_device_id, which takes the parent's registry id, so it has to exist
    # before any platform registers a slot.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Lion IRVT",
        model="TIDA-010086 8-channel battery tester",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _build_primary(
    hass: HomeAssistant, entry: ConfigEntry, slot_count: int
) -> Transport:
    """Build the transport that carries status and test-engine commands."""
    transport = entry.data.get(CONF_TRANSPORT, TRANSPORT_BLE)

    if transport == TRANSPORT_HTTP:
        from homeassistant.helpers.aiohttp_client import (  # noqa: PLC0415
            async_get_clientsession,
        )

        from .transport.http import HttpTransport  # noqa: PLC0415

        return HttpTransport(
            entry.data[CONF_HTTP_HOST],
            async_get_clientsession(hass),
            slot_count=slot_count,
        )

    if transport == TRANSPORT_CAN:
        from .transport.can import CanTransport  # noqa: PLC0415

        return CanTransport(
            entry.data.get(CONF_CAN_INTERFACE, "can0"),
            bitrate=entry.data.get(CONF_CAN_BITRATE, 500_000),
            slot_count=slot_count,
        )

    return await _build_ble(hass, entry, slot_count)


async def _build_ble(
    hass: HomeAssistant, entry: ConfigEntry, slot_count: int
) -> Transport:
    from homeassistant.components import bluetooth  # noqa: PLC0415
    from bleak_retry_connector import establish_connection  # noqa: PLC0415

    from .transport.ble import BleTransport  # noqa: PLC0415

    address: str = entry.data[CONF_ADDRESS]

    # Assigned below, once the transport exists: the callback has to reach it,
    # and the transport needs the factory that holds the callback.
    transport: BleTransport

    def _on_disconnect(client) -> None:
        transport.handle_disconnect(client)

    def _factory():
        # Looked up afresh on every connect, never captured. After a unit
        # reset the proxy may be a different one, or the device object stale.
        #
        # connectable=True so a passive-only proxy is not offered: this
        # integration needs to write, not merely observe advertisements.
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            raise TransportError(
                f"tester {address} is not in range of any Bluetooth adapter "
                "or proxy"
            )
        from bleak import BleakClient  # noqa: PLC0415

        return establish_connection(
            BleakClient,
            ble_device,
            entry.title or address,
            disconnected_callback=_on_disconnect,
            # Re-resolve the device on each attempt, so a retry can be routed
            # through whichever adapter or proxy now has the best signal.
            ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                hass, address, connectable=True
            )
            or ble_device,
        )

    transport = BleTransport(_factory, slot_count=slot_count)
    return transport


async def _build_register_transport(
    hass: HomeAssistant, entry: ConfigEntry, slot_count: int
) -> Transport | None:
    """Build the optional secondary link used only for register access.

    This is what gives a BLE deployment direct charge/discharge control when
    the proxy's firmware has no register characteristic.
    """
    primary = entry.data.get(CONF_TRANSPORT, TRANSPORT_BLE)

    if entry.data.get(CONF_CAN_INTERFACE) and primary != TRANSPORT_CAN:
        from .transport.can import CanTransport  # noqa: PLC0415

        return CanTransport(
            entry.data[CONF_CAN_INTERFACE],
            bitrate=entry.data.get(CONF_CAN_BITRATE, 500_000),
            slot_count=slot_count,
        )

    if entry.data.get(CONF_HTTP_HOST) and primary != TRANSPORT_HTTP:
        from homeassistant.helpers.aiohttp_client import (  # noqa: PLC0415
            async_get_clientsession,
        )

        from .transport.http import HttpTransport  # noqa: PLC0415

        return HttpTransport(
            entry.data[CONF_HTTP_HOST],
            async_get_clientsession(hass),
            slot_count=slot_count,
        )

    return None


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    coordinator: LionCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.device.async_disconnect()

    if not hass.data[DOMAIN]:
        async_unregister_services(hass)
        hass.data.pop(DOMAIN)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
