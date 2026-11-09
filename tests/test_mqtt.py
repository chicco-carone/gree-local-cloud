from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import aiomqtt
import pytest

from gree_local.mqtt import BrokerMqtt
from gree_local.server import Gateway, load_config
from gree_local.types import JsonObject


def configuration(tmp_path: Path) -> JsonObject:
    return {
        "device_ip": "192.0.2.20",
        "external_ip": "192.0.2.10",
        "module_mac": "aabbccddeeff",
        "unit_mac": "aabbccddeeff00",
        "name": "Test AC",
        "model": "Test",
        "mqtt": {"host": "localhost"},
        "private_dir": str(tmp_path / "data"),
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_discovery_toggle_leaves_plain_mqtt_state_and_availability(
    tmp_path: Path, enabled: bool
) -> None:
    config = configuration(tmp_path)
    config["ha_discovery"] = enabled
    gateway = Gateway(config)
    gateway.mqtt.publish = AsyncMock()
    gateway.properties = {"Pow": 0, "SetDeciTem": 235}
    await gateway.mqtt_ready()
    topics = [call.args[0] for call in gateway.mqtt.publish.call_args_list]
    assert any(topic.startswith("homeassistant/") for topic in topics) == enabled
    assert gateway.base + "/state" in topics
    assert gateway.base + "/availability" in topics
    gateway.mqtt.publish.reset_mock()
    await gateway.mqtt_command("homeassistant/status", "online")
    assert gateway.mqtt.publish.called == enabled


async def test_retained_command_is_never_replayed(tmp_path: Path) -> None:
    gateway = Gateway(configuration(tmp_path))
    gateway.set_properties = AsyncMock()
    gateway.online = True
    await gateway.mqtt_command(gateway.base + "/command/mode", "heat", retained=True)
    gateway.set_properties.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_direct_transport_auth_subscriptions_messages_and_graceful_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from gree_local import mqtt

    password_file = tmp_path / "password"
    password_file.write_text("secret-from-file\n")
    config = configuration(tmp_path)
    config.update(ha_discovery=enabled, temperature_topic="room/temperature")
    config["mqtt"].update(
        username="test-user", password="unused", password_file=str(password_file)
    )
    base = "gree_local/aabbccddeeff00"
    ready, on_message, on_temperature = AsyncMock(), AsyncMock(), AsyncMock()
    subscriptions = []
    published = []
    client_options: JsonObject = {}
    queue: asyncio.Queue[aiomqtt.Message] = asyncio.Queue()

    class Client:
        def __init__(self, **kwargs: object) -> None:
            client_options.update(kwargs)

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def subscribe(self, topic: str, **kwargs: object) -> None:
            subscriptions.append(topic)

        async def publish(self, topic: str, payload: str, **kwargs: object) -> None:
            published.append((topic, payload, kwargs))

        @property
        def messages(self) -> AsyncIterator[aiomqtt.Message]:
            async def messages() -> AsyncIterator[aiomqtt.Message]:
                while True:
                    yield await queue.get()

            return messages()

    monkeypatch.setattr(mqtt.aiomqtt, "Client", Client)
    transport = BrokerMqtt(config, base, ready, on_message, on_temperature)
    task = asyncio.create_task(transport.run())
    try:
        await asyncio.wait_for(transport.connected.wait(), 1)
        ready.assert_awaited_once()
        assert client_options["username"] == "test-user"
        assert client_options["password"] == "secret-from-file"
        assert client_options["identifier"] == "gree-local-aabbccddeeff00"
        will = client_options["will"]
        assert will.topic == base + "/availability"
        assert will.payload == "offline" and will.retain and will.qos == 1
        assert ("homeassistant/status" in subscriptions) == enabled
        assert base + "/command/#" in subscriptions
        assert "room/temperature" in subscriptions
        await transport.receive(
            aiomqtt.Message(
                topic="room/temperature",
                payload=b"21.5",
                qos=1,
                retain=True,
                mid=1,
                properties=None,
            )
        )
        on_temperature.assert_awaited_once_with("21.5")
        await transport.receive(
            aiomqtt.Message(
                topic=base + "/command/mode",
                payload=b"cool",
                qos=1,
                retain=True,
                mid=2,
                properties=None,
            )
        )
        on_message.assert_awaited_once_with(base + "/command/mode", "cool", True)
        await transport.receive(
            aiomqtt.Message(
                topic="invalid",
                payload=b"\xff",
                qos=1,
                retain=False,
                mid=3,
                properties=None,
            )
        )
        assert on_message.await_count == 1
        await transport.publish(base + "/state", {"mode": "cool"})
        assert json.loads(published[-1][1]) == {"mode": "cool"}
        assert published[-1][2] == {"qos": 1, "retain": True}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert published[-1][:2] == (base + "/availability", "offline")
    assert not transport.connected.is_set()
    assert transport.client is None


def test_broker_environment_and_discovery_boolean(
    monkeypatch: pytest.MonkeyPatch, bridge_environment: None
) -> None:
    for name, value in {
        "MQTT_HOST": "broker.local",
        "MQTT_PORT": "8883",
        "MQTT_USERNAME": "mqtt-user",
        "MQTT_PASSWORD": "mqtt-password",
        "MQTT_TLS": "true",
        "HA_DISCOVERY": "false",
        "TEMPERATURE_TOPIC": "room/temperature",
    }.items():
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config["mqtt"]["host"] == "broker.local"
    assert config["mqtt"]["port"] == 8883
    assert config["mqtt"]["username"] == "mqtt-user"
    assert config["mqtt"]["password"] == "mqtt-password"
    assert config["mqtt"]["tls"] is True
    assert config["ha_discovery"] is False
    assert config["temperature_topic"] == "room/temperature"
    # Device login credentials are separate from upstream broker credentials.
    assert config["mqtt_username"] != "mqtt-user"
    assert config["mqtt_password"] != "mqtt-password"
    monkeypatch.setenv("HA_DISCOVERY", "typo")
    with pytest.raises(ValueError, match="HA_DISCOVERY"):
        load_config()


def test_menu_language_environment(
    monkeypatch: pytest.MonkeyPatch, bridge_environment: None
) -> None:
    monkeypatch.delenv("LANGUAGE", raising=False)
    assert load_config()["language"] == "en"
    monkeypatch.setenv("LANGUAGE", "it")
    assert load_config()["language"] == "it"
    monkeypatch.setenv("LANGUAGE", "fr")
    with pytest.raises(ValueError, match="language must be en or it"):
        load_config()


def test_environment_only_startup_defaults(
    monkeypatch: pytest.MonkeyPatch, bridge_environment: None
) -> None:
    monkeypatch.setenv("GREE_LISTEN_PORT", "1984")
    monkeypatch.setenv("GREE_POLL_INTERVAL", "15")
    monkeypatch.setenv("GREE_NAME", "Bedroom AC")
    monkeypatch.setenv("GREE_DNS_NAMES", " eu.dis.gree.com, mqtt-eu.gree.com ")
    config = load_config()
    assert config["name"] == "Bedroom AC"
    assert config["listen_port"] == 1984
    assert config["poll_interval"] == 15
    assert config["dns_names"] == ["eu.dis.gree.com", "mqtt-eu.gree.com"]
    from pathlib import Path

    assert Path(config["certificate"]).is_file()
    assert Path(config["dispatcher_certificate"]).is_file()
    monkeypatch.delenv("GREE_MODULE_MAC")
    with pytest.raises(ValueError, match="Missing module_mac"):
        load_config()
