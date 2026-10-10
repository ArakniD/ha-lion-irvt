"""Reconnection: the link has to heal itself without a reload.

A unit that resets, a proxy that drops, or a client that goes out of range all
leave the BLE transport holding a client that can no longer talk. The old
transport kept reusing it, so every later poll failed until the integration was
reloaded by hand. These tests kill the link the way a real one dies and check
that the next operation recovers on its own.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.lion_lvrt.transport import ble as ble_mod
from custom_components.lion_lvrt.transport.base import TransportError
from custom_components.lion_lvrt.transport.ble import BleTransport
from tests.simulator.fake_ble import FakeBleClient
from tests.simulator.unit import SimulatedUnit


class _Factory:
    """Hands out a fresh fake client per connect, like establish_connection."""

    def __init__(self, unit: SimulatedUnit) -> None:
        self.unit = unit
        self.clients: list[FakeBleClient] = []
        self.fail_next = 0

    def __call__(self) -> FakeBleClient:
        if self.fail_next:
            self.fail_next -= 1
            raise TransportError("tester is not in range of any Bluetooth adapter")
        client = FakeBleClient(self.unit)
        self.clients.append(client)
        return client

    @property
    def current(self) -> FakeBleClient:
        return self.clients[-1]


async def _up() -> tuple[BleTransport, _Factory]:
    factory = _Factory(SimulatedUnit())
    transport = BleTransport(factory)
    await transport.async_connect()
    await transport.async_poll()
    return transport, factory


async def test_a_dead_link_is_replaced_on_the_next_poll() -> None:
    """The reported bug: after a link failure every poll failed until reload.

    The poll itself now notices the dead client and reconnects, so it succeeds
    straight away rather than failing once first.
    """
    transport, factory = await _up()
    factory.current.drop_link()

    snapshot = await transport.async_poll()

    assert len(factory.clients) == 2, "a fresh client must replace the dead one"
    assert len(snapshot.slots) == 8


async def test_failing_reads_on_a_client_that_claims_to_be_connected() -> None:
    """The case behind "GATT error 133": the stack still reports connected but
    every read fails. Nothing else clears that client, so a failed read must."""
    transport, factory = await _up()
    factory.current.break_reads()

    with pytest.raises(TransportError, match="read of"):
        await transport.async_poll()
    assert transport._client is None, "the broken client must be discarded"

    snapshot = await transport.async_poll()  # reconnects, and now works

    assert len(factory.clients) == 2
    assert len(snapshot.slots) == 8


async def test_connect_replaces_a_client_that_reports_disconnected() -> None:
    """A drop the stack reported, with no failed read to reveal it."""
    transport, factory = await _up()
    factory.current.drop_link()

    await transport.async_connect()

    assert len(factory.clients) == 2
    assert (await transport.async_poll()).slots


async def test_a_live_link_is_not_reconnected() -> None:
    transport, factory = await _up()
    for _ in range(3):
        await transport.async_connect()
        await transport.async_poll()
    assert len(factory.clients) == 1


async def test_reconnect_resubscribes_to_notifications() -> None:
    """A new connection starts with no subscriptions; without this the live
    feed would stay silent for good after the first drop."""
    transport, factory = await _up()
    seen: list[int] = []
    await transport.async_start_notify(lambda snap: seen.extend(sorted(snap.slots)))
    assert factory.current.subscribed

    factory.current.drop_link()
    await transport.async_connect()

    assert len(factory.clients) == 2
    assert factory.current.subscribed, "the new client was never subscribed"

    factory.unit.slots[3].voltage_v = 3.9
    factory.current.push_slot_notification(3)
    assert 3 in seen


async def test_a_hung_read_times_out_instead_of_wedging(monkeypatch) -> None:
    """bleak has no timeout of its own on a read. A unit that resets mid-poll
    can leave one hanging until the link's supervision timeout."""
    monkeypatch.setattr(ble_mod, "OPERATION_TIMEOUT_S", 0.05)
    transport, factory = await _up()
    factory.current.hang()

    with pytest.raises(TransportError, match="TimeoutError|failed"):
        await asyncio.wait_for(transport.async_poll(), timeout=2)

    # ...and the hung client is gone, so the next connect replaces it.
    await transport.async_connect()
    assert len(factory.clients) == 2
    assert (await transport.async_poll()).slots


async def test_failure_to_reconnect_is_a_transport_error_and_retries() -> None:
    """The unit being away must surface as TransportError, which the coordinator
    turns into UpdateFailed and retries - not as an exception that ends it."""
    transport, factory = await _up()
    factory.current.drop_link()
    factory.fail_next = 2

    for _ in range(2):
        with pytest.raises(TransportError):
            await transport.async_connect()

    await transport.async_connect()  # unit is back
    assert (await transport.async_poll()).slots


async def test_a_foreign_exception_from_the_factory_becomes_a_transport_error() -> None:
    """bleak raises its own types; home assistant only retries ours."""
    transport, factory = await _up()
    factory.current.drop_link()

    def boom():
        raise OSError("adapter vanished")

    transport._client_factory = boom
    with pytest.raises(TransportError, match="adapter vanished"):
        await transport.async_connect()


async def test_a_refused_command_does_not_drop_a_healthy_link() -> None:
    """ATT 0x0E means "refused" with a link that is perfectly fine; tearing the
    connection down for it would turn every refusal into a reconnect."""
    from custom_components.lion_lvrt.transport.base import UnitRefusedError

    transport, factory = await _up()
    with pytest.raises(UnitRefusedError):
        await transport.async_start(0)  # unconfigured, so refused

    assert len(factory.clients) == 1
    await transport.async_connect()
    assert len(factory.clients) == 1


async def test_late_disconnect_callback_cannot_kill_its_replacement() -> None:
    """The callback from a client we already replaced must be ignored."""
    transport, factory = await _up()
    old = factory.current
    old.drop_link()
    await transport.async_connect()
    new = factory.current

    transport.handle_disconnect(old)

    assert transport._client is new
    assert (await transport.async_poll()).slots


async def test_disconnect_callback_forces_a_reconnect() -> None:
    transport, factory = await _up()
    transport.handle_disconnect(factory.current)
    assert transport._client is None

    await transport.async_connect()
    assert len(factory.clients) == 2


async def test_concurrent_connects_make_one_client() -> None:
    """A poll and a command arriving together after a drop must not race two
    connections at a unit that accepts only one."""
    transport, factory = await _up()
    factory.current.drop_link()

    await asyncio.gather(*(transport.async_connect() for _ in range(5)))

    assert len(factory.clients) == 2


async def test_unloading_stops_a_late_reconnect_resubscribing() -> None:
    """After unload the coordinator is gone; resubscribing its callback on a
    later reconnect would feed a dead object."""
    transport, factory = await _up()
    await transport.async_start_notify(lambda snap: None)
    await transport.async_disconnect()

    assert transport._notify_cb is None
    assert transport._client is None
