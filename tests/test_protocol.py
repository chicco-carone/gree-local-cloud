from __future__ import annotations

import asyncio
import hashlib
import struct
from pathlib import Path
from unittest.mock import Mock

import pytest
from Crypto.Cipher import AES

from gree_local.climate import commands_for, discovery, state_from_properties
from gree_local.server import Gateway, decrypt, encrypt, mqtt_packet, take_mqtt_packet
from gree_local.tls import Connection, handshake, prf, record
from gree_local.weak_rng import observe, rng_bytes, tick_mask


def test_commercial_half_degree_command_does_not_truncate_fraction() -> None:
    commands = commands_for("temperature", "27.5", {"HalfTemEn": 1, "SetDeciTem": 280})
    assert commands == [{"SetDeciTem": 275}]
    assert state_from_properties({"Pow": 0, "SetDeciTem": 275})["temperature"] == 27.5


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "31", "27.3"])
def test_invalid_temperature_never_reaches_device(value: str) -> None:
    with pytest.raises(ValueError):
        commands_for("temperature", value, {"HalfTemEn": 1, "SetDeciTem": 280})


def test_mode_is_applied_before_power() -> None:
    assert commands_for("mode", "heat", {}) == [{"Mod": 4}, {"Pow": 1}]
    assert commands_for("mode", "off", {}) == [{"Pow": 0}]


def test_real_mqtt_discovery_uses_existing_entity_and_implicit_none_preset() -> None:
    configs = discovery("test00", "Gree Local", "WB04BRT13")
    climate = configs["homeassistant/climate/gree_local_test00_climate/config"]
    assert climate["unique_id"] == "gree_local_test00_climate"
    assert "none" not in climate["preset_modes"]
    assert climate["temp_step"] == 0.5
    assert climate["mode_command_topic"] == "gree_local/test00/command/mode"


def test_mqtt_packet_fragmentation_and_coalescing() -> None:
    packet = mqtt_packet(0x30, b"x" * 400)
    for split in [1, 2, 100, len(packet) - 1]:
        assert take_mqtt_packet(packet[:split]) is None
    parsed = take_mqtt_packet(packet + mqtt_packet(0xC0))
    assert parsed is not None
    header, data, remaining = parsed
    assert header == 0x30 and data == b"x" * 400
    assert take_mqtt_packet(remaining) == (0xC0, b"", b"")
    with pytest.raises(ValueError):
        take_mqtt_packet(b"\x30\xff\xff\xff\xff")


def test_gree_encryption_preserves_utf8_and_rejects_corruption() -> None:
    obj = {"t": "cmd", "name": "Living room", "opt": ["SetDeciTem"], "p": [275]}
    assert decrypt(encrypt(obj)) == obj
    with pytest.raises(ValueError):
        decrypt("invalid base64")


def test_firmware_rng_recovery_contains_original_tick() -> None:
    tick = 0x55667788
    random = rng_bytes(0x12345678, tick_mask(tick), 32)
    state, candidates = observe(random)
    assert state == struct.unpack("<I", random[-4:])[0]
    assert tick in candidates
    assert len(candidates) == 512
    with pytest.raises(ValueError):
        observe(bytes(range(32)))


async def read_record(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(5)
    return header[0], await reader.readexactly(int.from_bytes(header[3:5], "big"))


def seal(key: bytes, iv: bytes, seq: int, kind: int, plaintext: bytes) -> bytes:
    explicit = seq.to_bytes(8, "big")
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv + explicit)
    cipher.update(
        explicit + bytes([kind]) + b"\x03\x03" + len(plaintext).to_bytes(2, "big")
    )
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return record(kind, explicit + ciphertext + tag)


def unseal(key: bytes, iv: bytes, seq: int, kind: int, data: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv + data[:8])
    cipher.update(
        seq.to_bytes(8, "big")
        + bytes([kind])
        + b"\x03\x03"
        + (len(data) - 24).to_bytes(2, "big")
    )
    return cipher.decrypt_and_verify(data[8:-16], data[-16:])


async def test_device_tls_handshake_finished_and_bidirectional_records() -> None:
    completed = asyncio.get_running_loop().create_future()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            conn = await Connection(
                reader, writer, str(Path("certs/gree-device.pem").resolve())
            ).accept()
            assert await conn.read() == b"ping"
            await conn.write(b"pong")
            completed.set_result(None)
        except Exception as exc:
            completed.set_exception(exc)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        tick = 0x55667788
        cr = rng_bytes(0x12345678, tick_mask(tick), 32)
        ch = handshake(1, b"\x03\x03" + cr + b"\x00\x00\x02\x00\x9d\x01\x00\x00\x00")
        writer.write(record(22, ch))
        kind, sh = await read_record(reader)
        assert kind == 22
        sr = sh[6:38]
        state = struct.unpack("<I", cr[-4:])[0]
        pms = b"\x03\x03" + rng_bytes(state, tick_mask(tick + 13), 46)
        master = prf(pms, b"master secret", cr + sr, 48)
        keys = prf(master, b"key expansion", sr + cr, 72)
        client_key, server_key, client_iv, server_iv = (
            keys[:32],
            keys[32:64],
            keys[64:68],
            keys[68:72],
        )
        # The device sends an RSA-encrypted PMS; this server derives it from RNG.
        cke = handshake(16, b"\x01\x00" + b"\x42" * 256)
        transcript = ch + sh + cke
        cf = handshake(
            20, prf(master, b"client finished", hashlib.sha384(transcript).digest(), 12)
        )
        writer.write(
            record(22, cke)
            + record(20, b"\x01")
            + seal(client_key, client_iv, 0, 22, cf)
        )
        await writer.drain()
        assert await read_record(reader) == (20, b"\x01")
        kind, encrypted = await read_record(reader)
        sf = unseal(server_key, server_iv, 0, kind, encrypted)
        assert sf == handshake(
            20,
            prf(
                master, b"server finished", hashlib.sha384(transcript + cf).digest(), 12
            ),
        )
        writer.write(seal(client_key, client_iv, 1, 23, b"ping"))
        await writer.drain()
        kind, encrypted = await read_record(reader)
        assert unseal(server_key, server_iv, 1, kind, encrypted) == b"pong"
        with pytest.raises(ValueError):
            unseal(
                server_key,
                server_iv,
                1,
                kind,
                encrypted[:-1] + bytes([encrypted[-1] ^ 1]),
            )
        await completed
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


def test_container_config_persists_generated_device_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bridge_environment: None
) -> None:
    from gree_local.server import load_config

    monkeypatch.setenv("GREE_SERVER_IP", "192.0.2.30")
    first = load_config()
    second = load_config()
    assert first["external_ip"] == "192.0.2.30"
    assert first["mqtt_username"] == second["mqtt_username"]
    assert first["mqtt_password"] == second["mqtt_password"]
    assert len(first["mqtt_password"]) == 16
    assert (tmp_path / "data/mqtt-credentials.json").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("GREE_SERVER_IP", "0.0.0.0")
    with pytest.raises(ValueError):
        load_config()


async def test_acknowledgement_alone_does_not_confirm_physical_setting(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock

    from gree_local.server import Gateway

    gateway = Gateway(
        {
            "module_mac": "test",
            "unit_mac": "test00",
            "private_dir": str(tmp_path),
            "mqtt": {"host": "localhost"},
        }
    )
    gateway.command = AsyncMock(return_value={"t": "res", "r": 200})
    gateway.query = AsyncMock()
    gateway.properties = {"SetDeciTem": 270}
    with pytest.raises(RuntimeError, match="readback differs"):
        await gateway.set_properties([{"SetDeciTem": 275}])
    gateway.query.assert_awaited_once()


@pytest.mark.parametrize(
    "field,property_name",
    [("swing_mode", "SwUpDn"), ("swing_horizontal_mode", "SwingLfRig")],
)
@pytest.mark.parametrize("language", ["en", "it"])
def test_swing_labels_round_trip_to_original_motor_positions(
    field: str, property_name: str, language: str
) -> None:
    from jinja2 import Environment, StrictUndefined

    from gree_local.climate import HORIZONTAL, SWINGS

    config = discovery("test00", "Test", "Test", language=language)[
        "homeassistant/climate/gree_local_test00_climate/config"
    ]
    environment = Environment(undefined=StrictUndefined)
    values = SWINGS if field == "swing_mode" else HORIZONTAL
    for index, value in enumerate(values):
        label = environment.from_string(config[field + "_state_template"]).render(
            value_json={field: value}
        )
        assert label == config[field + "s"][index]
        assert "_" not in label
        if index == 0:
            assert label == ("Predefinita" if language == "it" else "Default")
        command = environment.from_string(config[field + "_command_template"]).render(
            value=label
        )
        assert command == value
        assert commands_for(field, command, {}) == [{property_name: index}]
        assert (
            environment.from_string(config[field + "_command_template"]).render(
                value=value
            )
            == value
        )


def make_test_gateway(tmp_path: Path) -> Gateway:
    from gree_local.server import Gateway

    gateway = Gateway(
        {
            "module_mac": "test",
            "unit_mac": "test00",
            "private_dir": str(tmp_path),
            "mqtt": {"host": "localhost"},
        }
    )
    gateway.conn = Mock(spec=Connection)
    gateway.authenticated = True
    return gateway


async def test_physical_controller_binary_push_schedules_refresh_without_waiting_for_reply(
    tmp_path: Path,
) -> None:
    import json

    from gree_local.server import mqtt_string

    gateway = make_test_gateway(tmp_path)
    topic = "binStatus/test/test00"
    await asyncio.wait_for(
        gateway.packet(
            None,
            0x30,
            mqtt_string(topic) + json.dumps({"t": topic, "binStatus": "AA=="}).encode(),
        ),
        0.5,
    )
    assert gateway.refresh_event.is_set()
    # A sibling indoor unit's report must not trigger our unit's refresh.
    gateway.refresh_event.clear()
    other = "binStatus/test/other"
    await gateway.packet(
        None,
        0x30,
        mqtt_string(other) + json.dumps({"t": other, "binStatus": "AA=="}).encode(),
    )
    assert not gateway.refresh_event.is_set()


async def test_push_refresh_is_immediate_coalesced_and_does_not_lose_events_in_flight(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock

    gateway = make_test_gateway(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    second = asyncio.Event()
    calls = 0

    async def query() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        else:
            second.set()

    gateway.query = AsyncMock(side_effect=query)
    task = asyncio.create_task(gateway.refresh_from_push())
    try:
        for _ in range(10):
            gateway.refresh_event.set()
        await asyncio.wait_for(started.wait(), 1)
        assert gateway.query.await_count == 1
        gateway.refresh_event.set()
        release.set()
        await asyncio.wait_for(second.wait(), 1)
        assert gateway.query.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
