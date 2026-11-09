import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gree_local import server


async def test_startup_registers_handlers_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, bridge_environment: None
) -> None:
    gateway = server.Gateway(server.load_config())
    monkeypatch.setattr(server, "Gateway", lambda config: gateway)
    monkeypatch.setattr(gateway.mqtt, "run", AsyncMock())
    transport = MagicMock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "create_datagram_endpoint", AsyncMock(return_value=(transport, None))
    )
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def serve_forever() -> None:
        started.set()
        await stopped.wait()

    device_listener, control_listener, dispatcher_listener = (
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    device_listener.serve_forever = AsyncMock(side_effect=serve_forever)
    start_server = AsyncMock(
        side_effect=[device_listener, control_listener, dispatcher_listener]
    )
    monkeypatch.setattr(asyncio, "start_server", start_server)
    task = asyncio.create_task(server.run(server.load_config()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert [call.args[0] for call in start_server.await_args_list] == [
            gateway.handle,
            gateway.control,
            gateway.dispatcher,
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    transport.close.assert_called_once()
