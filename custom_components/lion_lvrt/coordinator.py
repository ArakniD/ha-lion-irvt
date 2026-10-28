"""Update coordinator.

Polls the unit and fans the result out to every entity. Two concerns beyond
the usual:

**The poll is also the safety keepalive.** The unit pauses every running slot
if no host command arrives within ``eHostWatchdog_s``. A register read counts,
so the poll interval must stay comfortably inside that timeout - and if it
cannot, the right response is to tell the operator, not to quietly disable
the watchdog.

**Notifications supplement the poll, they do not replace it.** Delivery is
best-effort: a notification is dropped without retry or sequence number when
the device's buffer pool is exhausted. So the poll continues underneath at a
slower rate and the notification provides the low-latency path.

**Finished cycles are detected here, not in an entity.** Both the poll and
the push go through :meth:`LionCoordinator._observe`, because a charge that
ends between polls should be recorded when the push says so rather than up
to a poll interval later. Entities are free to come and go; the cycle log
must not depend on one being loaded.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .assignments import AssignmentStore
from .const import (
    CONF_ADDRESS,
    CONF_CAN_INTERFACE,
    CONF_HTTP_HOST,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    EVENT_CYCLE_COMPLETED,
    WATCHDOG_FEED_MARGIN,
)
from .cycles import CycleWatcher, normalise_address
from .device import LionDevice
from .transport.base import DeviceSnapshot, TransportError

_LOGGER = logging.getLogger(__name__)


class LionCoordinator(DataUpdateCoordinator[DeviceSnapshot]):
    """Keeps one unit's snapshot current."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: LionDevice,
        entry: ConfigEntry,
        *,
        scan_interval_s: float = DEFAULT_SCAN_INTERVAL_S,
    ) -> None:
        self.device = device
        self.entry = entry
        self._warned_about_watchdog = False
        #: False from the first failed poll until the next good one, so the
        #: recovery is logged once and an outage is not logged every cycle.
        self._link_up = True
        #: Cycle detection and the cell codes a finished cycle is filed
        #: under. The store is loaded by async_setup_entry before the first
        #: refresh, so a cycle that ends on the very first poll after a
        #: restart still finds the assignment it belongs to.
        self._cycles = CycleWatcher()
        self.assignments = AssignmentStore(hass, entry.entry_id)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan_interval_s),
        )

    # --- identity ---------------------------------------------------------

    @property
    def address(self) -> str:
        """The tester's Bluetooth address, normalised for comparison.

        What a scanned slot label is matched against. Falls back to the HTTP
        host or CAN interface on a tester configured without Bluetooth, so a
        label can still name one unit out of several.
        """
        data = self.entry.data
        for key in (CONF_ADDRESS, CONF_HTTP_HOST, CONF_CAN_INTERFACE):
            value = data.get(key)
            if value:
                return normalise_address(str(value))
        return ""


    async def _async_update_data(self) -> DeviceSnapshot:
        try:
            # Reconnects here if the link was lost. This runs on every poll, so
            # a unit that is reset or out of range is picked up again as soon
            # as it comes back, with no reload.
            await self.device.async_ensure_connected()
            snapshot = await self.device.async_poll()
        except TransportError as err:
            self._link_up = False
            raise UpdateFailed(f"could not reach the unit: {err}") from err
        except Exception as err:  # noqa: BLE001
            # Anything else a transport lets escape (a bleak error, a timeout)
            # would be logged as an unexpected failure and could stop the
            # refresh loop. Treat it as the link failing, which it is.
            self._link_up = False
            raise UpdateFailed(
                f"could not reach the unit: {str(err) or type(err).__name__}"
            ) from err

        if not self._link_up:
            _LOGGER.info("Connection to %s restored", self.entry.title)
        self._link_up = True
        self._check_watchdog_margin(snapshot)
        self._observe(snapshot)
        return snapshot

    def _check_watchdog_margin(self, snapshot: DeviceSnapshot) -> None:
        """Warn if polling is too slow to keep the unit's watchdog fed.

        Warned once rather than every cycle: this is a configuration problem,
        and repeating it every few seconds would bury the log.
        """
        if self._warned_about_watchdog or self.update_interval is None:
            return
        timeout = snapshot.unit.watchdog_timeout_s
        if timeout <= 0:
            return
        interval = self.update_interval.total_seconds()
        if interval + WATCHDOG_FEED_MARGIN >= timeout:
            _LOGGER.warning(
                "Poll interval (%.0fs) is close to the unit's host watchdog "
                "(%.0fs). If a poll is delayed the unit will pause every "
                "running slot. Shorten the scan interval or raise the "
                "watchdog timeout",
                interval,
                timeout,
            )
            self._warned_about_watchdog = True

    def handle_push(self, snapshot: DeviceSnapshot) -> None:
        """Accept a notification-driven snapshot between polls.

        Deliberately not ``async_set_updated_data``: that cancels and
        reschedules the poll timer, and the unit-status notification arrives
        every second. Every push therefore pushed the next poll out by a full
        interval and the poll never ran, so anything a notification does not
        carry - every slot that is not running - went stale for good.
        Updating the data and the listeners directly leaves the timer alone.
        """
        self.data = snapshot
        self._observe(snapshot)
        self.async_update_listeners()

    # --- completed cycles -------------------------------------------------

    def _observe(self, snapshot: DeviceSnapshot) -> None:
        """Fire an event for every charge or discharge that just finished.

        Called from the poll and the push alike. A notification carries only
        the slots that changed, which is exactly right here: an unmentioned
        slot has not moved, so its open segment should stay open rather than
        be closed by a snapshot that does not include it.
        """
        for cycle in self._cycles.observe(snapshot):
            self._emit(cycle)

    def _emit(self, cycle: dict) -> None:
        slot = cycle["slot"]
        assignment = self.assignments.data.get(slot)
        # The slot's cell profile, so the event stands on its own: whoever
        # reads it later should not have to join it against configuration
        # that may since have changed.
        config = self.device.cached_config(slot)

        event = {
            **cycle,
            "entry_id": self.entry.entry_id,
            "tester": self.entry.title,
            "address": self.address,
            # The timestamp is Home Assistant's, not the tester's: the device
            # has no real-time clock and reports only uptime.
            "timestamp": dt_util.utcnow().isoformat(),
            "code": assignment.code if assignment else "",
            "code_assigned_at": assignment.assigned_at if assignment else "",
            "serial": config.serial if config else "",
            "model": config.model_name if config else "",
            "chemistry": config.chemistry.name.lower() if config else "",
            "capacity_mah": config.capacity_mah if config else 0.0,
        }

        self.assignments.data.record(dict(event))
        self.assignments.save()
        self.hass.bus.async_fire(EVENT_CYCLE_COMPLETED, event)
        _LOGGER.info(
            "Slot %d %s finished (%s): %.1f mAh, %.1f mWh in %.0f s%s",
            cycle["slot_number"],
            cycle["direction"],
            cycle["outcome"],
            cycle["mah"],
            cycle["mwh"],
            cycle["seconds"],
            f", cell {event['code']}" if event["code"] else " (no cell code assigned)",
        )
