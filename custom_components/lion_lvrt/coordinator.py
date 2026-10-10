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
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    WATCHDOG_FEED_MARGIN,
)
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

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan_interval_s),
        )

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
        self.async_update_listeners()
