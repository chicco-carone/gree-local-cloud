"""Direct MQTT broker transport, independent of Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiomqtt

from gree_local.types import JsonObject

_LOG = logging.getLogger(__name__)


class BrokerMqtt:
    def __init__(
        self,
        config: JsonObject,
        base: str,
        on_ready: Callable[[], Awaitable[None]],
        on_message: Callable[[str, str, bool], Awaitable[None]],
        on_temperature: Callable[[str], Awaitable[None]],
    ) -> None:
        self.config = config
        self.base = base
        self.on_ready = on_ready
        self.on_message = on_message
        self.on_temperature = on_temperature
        self.discovery = config.get("ha_discovery", True)
        self.temperature_topic = config.get("temperature_topic")
        self.client: aiomqtt.Client | None = None
        self.connected = asyncio.Event()

    async def publish(self, topic: str, payload: object, retain: bool = True) -> None:
        if not self.connected.is_set() or self.client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"))
        try:
            await self.client.publish(topic, payload, qos=1, retain=retain)
        except aiomqtt.MqttError as exc:
            _LOG.warning("MQTT publish failed for %s: %s", topic, exc)

    async def receive(self, message: aiomqtt.Message) -> None:
        try:
            payload = bytes(message.payload).decode()
            topic = str(message.topic)
            if topic == self.temperature_topic:
                await self.on_temperature(payload)
            else:
                await self.on_message(topic, payload, message.retain)
        except (UnicodeError, ValueError, TypeError) as exc:
            _LOG.warning(
                "Discarding invalid MQTT message on %s: %s", message.topic, exc
            )

    async def run(self) -> None:
        broker = self.config["mqtt"]
        tls = (
            ssl.create_default_context(cafile=broker.get("ca_file"))
            if broker.get("tls")
            else None
        )
        while True:
            try:
                password = broker.get("password") or None
                if broker.get("password_file"):
                    password = Path(broker["password_file"]).read_text().strip()
                async with aiomqtt.Client(
                    hostname=broker["host"],
                    port=broker.get("port", 8883 if tls else 1883),
                    username=broker.get("username") or None,
                    password=password,
                    identifier=broker.get("client_id")
                    or "gree-local-" + self.base.rsplit("/", 1)[-1],
                    tls_context=tls,
                    keepalive=30,
                    will=aiomqtt.Will(
                        self.base + "/availability", "offline", qos=1, retain=True
                    ),
                ) as client:
                    self.client = client
                    try:
                        await client.subscribe(self.base + "/command/#", qos=1)
                        if self.discovery:
                            await client.subscribe("homeassistant/status", qos=1)
                        if self.temperature_topic:
                            await client.subscribe(self.temperature_topic, qos=1)
                        self.connected.set()
                        _LOG.info("MQTT broker connected")
                        await self.on_ready()
                        async for message in client.messages:
                            await self.receive(message)
                    finally:
                        # Graceful disconnect does not trigger the broker's last will.
                        await self.publish(self.base + "/availability", "offline")
                        self.connected.clear()
                        self.client = None
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError) as exc:
                _LOG.warning("MQTT connection failed: %s; retrying in 3 seconds", exc)
            finally:
                self.connected.clear()
                self.client = None
            await asyncio.sleep(3)
