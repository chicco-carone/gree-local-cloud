"""Device MQTT endpoint and Home Assistant MQTT Discovery bridge."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import math
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from Crypto.Cipher import AES

from gree_local.climate import COLS, commands_for, discovery, state_from_properties
from gree_local.dns import DeviceDNS
from gree_local.mqtt import BrokerMqtt
from gree_local.tls import Connection
from gree_local.types import JsonObject, Properties

_LOG = logging.getLogger(__name__)
DEFAULT_KEY = b"a3K8Bx%2r8Y7#xDh"


def encrypt(obj: JsonObject, key: bytes = DEFAULT_KEY) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    p = 16 - len(raw) % 16
    return base64.b64encode(
        AES.new(key, AES.MODE_ECB).encrypt(raw + bytes([p]) * p)
    ).decode()


def decrypt(pack: str, key: bytes = DEFAULT_KEY) -> JsonObject:
    data = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(pack, validate=True))
    if not data:
        raise ValueError("Empty encrypted packet")
    p = data[-1]
    if 1 <= p <= 16 and data[-p:] == bytes([p]) * p:
        data = data[:-p]
    else:
        data = data.rstrip(b"\0")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def mqtt_packet(header: int, payload: bytes = b"") -> bytes:
    n = len(payload)
    out = bytearray([header])
    while True:
        b = n % 128
        n //= 128
        out.append(b | (128 if n else 0))
        if not n:
            break
    return bytes(out) + payload


def mqtt_string(s: str) -> bytes:
    raw = s.encode()
    return len(raw).to_bytes(2, "big") + raw


def take_mqtt_packet(buf: bytes) -> tuple[int, bytes, bytes] | None:
    if len(buf) < 2:
        return None
    n = 0
    mult = 1
    pos = 1
    for _ in range(4):
        if pos >= len(buf):
            return None
        b = buf[pos]
        pos += 1
        n += (b & 127) * mult
        mult *= 128
        if not b & 128:
            break
    else:
        raise ValueError("Malformed MQTT remaining length")
    if n > 1024 * 1024:
        raise ValueError("MQTT message too large")
    if len(buf) < pos + n:
        return None
    return buf[0], buf[pos : pos + n], buf[pos + n :]


class Gateway:
    def __init__(self, config: JsonObject) -> None:
        self.config = config
        self.module = config["module_mac"]
        self.unit = config["unit_mac"]
        self.base = "gree_local/" + self.unit
        self.private = Path(config.get("private_dir", ".private"))
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.conn: Connection | None = None
        self.key: bytes | None = None
        self.lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.refresh_event = asyncio.Event()
        self.operation_lock = asyncio.Lock()
        self.pending: tuple[str, str | None, asyncio.Future[JsonObject]] | None = None
        self.sequence = 0
        self.properties: Properties = {}
        self.current_temperature: float | None = None
        self.last_seen = 0.0
        self.online = False
        self.authenticated = False
        self.last_error: str | None = None
        key_file = self.private / "device-key"
        if key_file.exists():
            self.key = key_file.read_bytes()
        self.mqtt = BrokerMqtt(
            config, self.base, self.mqtt_ready, self.mqtt_command, self.temperature
        )

    async def write(self, data: bytes) -> None:
        if not self.conn:
            raise ConnectionError("Device not connected")
        async with self.lock:
            await self.conn.write(data)

    async def publish_device(self, topic: str, obj: JsonObject) -> None:
        await self.write(
            mqtt_packet(
                0x30,
                mqtt_string(topic) + json.dumps(obj, separators=(",", ":")).encode(),
            )
        )

    async def command(
        self,
        inner: JsonObject,
        target: str | None = None,
        index: int | None = None,
        wait: bool = True,
    ) -> JsonObject | None:
        async with self.command_lock:
            if index is None:
                index = 0 if self.key else 1
            key = self.key if index == 0 and self.key else DEFAULT_KEY
            self.sequence += 1
            cid = "local-" + str(self.sequence)
            future: asyncio.Future[JsonObject] = (
                asyncio.get_running_loop().create_future()
            )
            self.pending = (cid, inner.get("t"), future)
            try:
                await self.publish_device(
                    "request/" + self.module,
                    {
                        "cid": cid,
                        "i": index,
                        "t": "pack",
                        "tcid": target or self.unit,
                        "uid": 0,
                        "pack": encrypt(inner, key),
                    },
                )
                _LOG.info("Device request %s %s", inner.get("t"), inner.get("opt", ""))
                if wait:
                    return await asyncio.wait_for(future, 8)
                return None
            finally:
                self.pending = None

    async def query(self) -> JsonObject:
        reply = await self.command({"t": "status", "cols": COLS})
        if reply is None or reply.get("t") != "dat" or reply.get("r", 200) != 200:
            raise RuntimeError("Status request rejected")
        return reply

    async def set_properties(self, updates: list[Properties]) -> None:
        async with self.operation_lock:
            expected: Properties = {}
            for update in updates:
                reply = await self.command(
                    {"t": "cmd", "opt": list(update), "p": list(update.values())}
                )
                if reply is None or reply.get("r", 200) != 200:
                    raise RuntimeError("Device rejected command")
                expected.update(update)
            await self.query()
            mismatch = {
                k: self.properties.get(k)
                for k, v in expected.items()
                if self.properties.get(k) != v
            }
            if mismatch:
                raise RuntimeError(
                    "Device readback differs from command: " + json.dumps(mismatch)
                )

    async def publish_state(self) -> None:
        if not self.properties:
            return
        await self.mqtt.publish(self.base + "/raw", self.properties)
        await self.mqtt.publish(
            self.base + "/state",
            state_from_properties(self.properties, self.current_temperature),
        )
        if self.current_temperature is not None:
            await self.mqtt.publish(
                self.base + "/room_temperature", str(self.current_temperature)
            )

    async def set_online(self, online: bool) -> None:
        self.online = online
        await self.mqtt.publish(
            self.base + "/availability", "online" if online else "offline"
        )

    async def mqtt_ready(self) -> None:
        if self.config.get("ha_discovery", True):
            # Remove the obsolete climate discovery topic from older versions.
            await self.mqtt.publish(
                "homeassistant/climate/gree_local_" + self.unit + "/config", ""
            )
            for topic, payload in discovery(
                self.unit,
                self.config["name"],
                self.config["model"],
                self.config.get("language", "en"),
            ).items():
                await self.mqtt.publish(topic, payload)
            _LOG.info("MQTT Discovery published")
        await self.publish_state()
        await self.mqtt.publish(
            self.base + "/availability", "online" if self.online else "offline"
        )

    async def temperature(self, value: str) -> None:
        try:
            temp = float(value)
        except (TypeError, ValueError):
            return
        if math.isfinite(temp):
            self.current_temperature = temp
            await self.publish_state()

    async def mqtt_command(
        self, topic: str, value: str, retained: bool = False
    ) -> None:
        if topic == "homeassistant/status":
            if self.config.get("ha_discovery", True) and value == "online":
                await self.mqtt_ready()
            return
        # Retained commands must never replay on reconnect.
        if retained:
            return
        if not topic.startswith(self.base + "/command/"):
            return
        field = topic.rsplit("/", 1)[-1]
        try:
            if not self.online:
                raise ConnectionError("AC is unavailable")
            updates = commands_for(field, value, self.properties)
            await self.set_properties(updates)
            self.last_error = None
            await self.mqtt.publish(
                self.base + "/last_command",
                {
                    "field": field,
                    "value": value,
                    "confirmed": True,
                    "timestamp": time.time(),
                },
                retain=False,
            )
        except Exception as exc:
            self.last_error = str(exc)
            _LOG.warning("MQTT command %s failed: %s", field, exc)
            await self.mqtt.publish(
                self.base + "/last_command",
                {"field": field, "confirmed": False, "error": str(exc)},
                retain=False,
            )

    async def send_time(self) -> None:
        if not self.key:
            return
        raw = b"key is ok"
        pad = 16 - len(raw) % 16
        proof = base64.b64encode(
            AES.new(self.key, AES.MODE_ECB).encrypt(raw + bytes([pad]) * pad)
        ).decode()
        await self.publish_device(
            "time",
            {
                "t": "tm",
                "i": 0,
                "timestamp": int(time.time() * 1000),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "encrypt": proof,
                "homeId": 0,
                "reportAnyway": False,
            },
        )

    async def dispatcher(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if writer.get_extra_info("peername")[0] != self.config["device_ip"]:
            writer.close()
            return
        try:
            conn = await asyncio.wait_for(
                Connection(
                    reader, writer, self.config["dispatcher_certificate"]
                ).accept(),
                30,
            )
            buf = b""
            while b"\n" not in buf and len(buf) < 8192:
                chunk = await asyncio.wait_for(conn.read(), 10)
                if not chunk:
                    raise ConnectionError("Dispatcher request incomplete")
                buf += chunk
            req = json.loads(buf.strip())
            if req.get("t") != "dis" or req.get("mac") != self.module:
                raise ValueError("Unexpected dispatcher request")
            ip = self.config["external_ip"]
            inner = {
                "t": "svr",
                "ip": ip,
                "ip2": ip,
                "Ip3": ip,
                "host": ip,
                "udpPort": 16386,
                "tcpPort": 16386,
                "protocol": "",
                "datHost": "",
                "datHostPort": 0,
                "mqttInfo": {
                    "host": ip,
                    "port": self.config.get("listen_port", 1982),
                    "username": self.config["mqtt_username"],
                    "password": self.config["mqtt_password"],
                },
            }
            reply = {
                "t": "pack",
                "i": 1,
                "uid": 0,
                "cid": "",
                "tcid": self.module,
                "pack": encrypt(inner),
            }
            await conn.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            _LOG.info("Local startup dispatcher answered for %s", self.module)
        except Exception as exc:
            _LOG.warning("Dispatcher: %s", exc)
        finally:
            writer.close()

    async def packet(self, conn: Connection | None, header: int, data: bytes) -> None:
        kind = header >> 4
        if kind == 1:
            n = int.from_bytes(data[:2], "big")
            pos = 2 + n + 4
            flags = data[3 + n]

            def read_string() -> str:
                nonlocal pos
                if pos + 2 > len(data):
                    raise ValueError("Truncated MQTT CONNECT")
                size = int.from_bytes(data[pos : pos + 2], "big")
                pos += 2
                if pos + size > len(data):
                    raise ValueError("Truncated MQTT CONNECT field")
                value = data[pos : pos + size].decode()
                pos += size
                return value

            ident = read_string()
            if flags & 4:
                read_string()
                read_string()
            username = read_string() if flags & 128 else ""
            password = read_string() if flags & 64 else ""
            if not hmac.compare_digest(ident, self.module):
                raise ValueError("Unexpected MQTT client ID")
            if not hmac.compare_digest(
                username, self.config["mqtt_username"]
            ) or not hmac.compare_digest(password, self.config["mqtt_password"]):
                raise ValueError("Device MQTT credentials rejected")
            self.authenticated = True
            _LOG.info("MQTT module connected")
            await self.write(mqtt_packet(0x20, b"\x00\x00"))
        elif not self.authenticated:
            raise ValueError("MQTT CONNECT required")
        elif kind == 8:
            pos = 2
            granted = []
            topics = []
            while pos < len(data):
                n = int.from_bytes(data[pos : pos + 2], "big")
                pos += 2
                topic = data[pos : pos + n].decode()
                pos += n
                if pos >= len(data):
                    raise ValueError("Truncated subscription")
                topics.append(topic)
                granted.append(min(data[pos], 1))
                pos += 1
            await self.write(mqtt_packet(0x90, data[:2] + bytes(granted)))
            _LOG.info("Module subscriptions: %s", topics)
            if "time" in topics:
                await self.send_time()
        elif kind == 3:
            n = int.from_bytes(data[:2], "big")
            topic = data[2 : 2 + n].decode()
            pos = 2 + n
            qos = (header >> 1) & 3
            if qos:
                ident = data[pos : pos + 2]
                pos += 2
                await self.write(mqtt_packet(0x40 if qos == 1 else 0x50, ident))
            obj = json.loads(data[pos:])
            inner = None
            if "pack" in obj:
                for key in [self.key, DEFAULT_KEY]:
                    if key:
                        try:
                            inner = decrypt(obj["pack"], key)
                            break
                        except (ValueError, KeyError, UnicodeDecodeError):
                            pass
            else:
                inner = obj
            if not isinstance(inner, dict):
                _LOG.warning("Undecodable device message on %s", topic)
                return
            # Private evidence is excluded from version control and contains keys.
            with (self.private / "local-messages.jsonl").open("a") as log:
                log.write(
                    json.dumps({"time": time.time(), "topic": topic, "inner": inner})
                    + "\n"
                )
            if inner.get("key"):
                self.key = inner["key"].encode()
                path = self.private / "device-key"
                path.write_bytes(self.key)
                path.chmod(0o600)
                _LOG.info("Device key obtained")
            self.last_seen = time.monotonic()
            if topic == f"binStatus/{self.module}/{self.unit}":
                # Physical controller edits arrive as binary telemetry, not
                # JSON state. Schedule a read outside this receive loop so
                # it remains free to consume the status reply.
                self.refresh_event.set()
            if inner.get("t") in ("dat", "res"):
                cols = inner.get("cols", inner.get("opt", []))
                values = inner.get("dat", inner.get("p", []))
                if len(cols) != len(values):
                    raise ValueError("Mismatched state columns")
                self.properties.update(
                    {k: v for k, v in zip(cols, values) if k in COLS}
                )
                if self.properties and not self.online:
                    await self.set_online(True)
                await self.publish_state()
            pending = self.pending
            if pending and topic.startswith("response/" + self.module + "/"):
                cid, expected, future = pending
                # Firmware echoes cid in tcid; legacy bind replies can omit it.
                if (
                    obj.get("tcid") == cid
                    or (expected == "bind" and inner.get("t") == "bindok")
                ) and not future.done():
                    future.set_result(inner)
            _LOG.info("Device %s on %s", inner.get("t"), topic)
        elif kind == 6:
            await self.write(mqtt_packet(0x70, data[:2]))
        elif kind == 12:
            await self.write(mqtt_packet(0xD0))
        elif kind == 14:
            raise ConnectionError("MQTT disconnect")

    async def initialize(self, conn: Connection) -> None:
        # Let CONNECT/SUBSCRIBE complete before sending control traffic.
        await asyncio.sleep(0.5)
        if self.conn is not conn:
            return
        if not self.key:
            await self.command(
                {"t": "bind", "mac": self.module, "uid": 0}, self.module, index=1
            )
        await self.send_time()
        await self.query()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if writer.get_extra_info("peername")[0] != self.config["device_ip"]:
            writer.close()
            return
        conn = Connection(reader, writer, self.config["certificate"])
        init = None
        try:
            await asyncio.wait_for(conn.accept(), 30)
            if self.conn:
                self.conn.writer.close()
            self.conn = conn
            self.authenticated = False
            buf = b""
            init = asyncio.create_task(self.initialize(conn))
            while chunk := await asyncio.wait_for(conn.read(), 100):
                buf += chunk
                while (parsed := take_mqtt_packet(buf)) is not None:
                    header, data, buf = parsed
                    await self.packet(conn, header, data)
        except Exception as exc:
            _LOG.warning("Device connection: %s: %s", type(exc).__name__, exc)
        finally:
            if init:
                init.cancel()
                await asyncio.gather(init, return_exceptions=True)
            if self.conn is conn:
                self.conn = None
                await self.set_online(False)
                if self.pending and not self.pending[2].done():
                    self.pending[2].set_exception(
                        ConnectionError("Device disconnected")
                    )
            writer.close()

    async def refresh_from_push(self) -> None:
        """Coalesce telemetry bursts and fetch their current state immediately."""
        while True:
            await self.refresh_event.wait()
            # A key press can produce several frames. Read their latest state
            # once after this brief debounce, not once per packet.
            await asyncio.sleep(0.05)
            self.refresh_event.clear()
            if self.conn and self.authenticated:
                try:
                    _LOG.info("Device change notification; refreshing state")
                    await self.query()
                except Exception as exc:
                    _LOG.warning("Push-triggered refresh failed: %s", exc)

    async def poll(self) -> None:
        while True:
            await asyncio.sleep(self.config.get("poll_interval", 20))
            if self.conn and self.authenticated:
                try:
                    await self.query()
                except Exception as exc:
                    _LOG.warning("Poll failed: %s", exc)
                    if time.monotonic() - self.last_seen > 60:
                        await self.set_online(False)

    async def heartbeat(self) -> None:
        while True:
            await self.mqtt.publish(
                self.base + "/heartbeat", str(int(time.time())), retain=False
            )
            if self.online and time.monotonic() - self.last_seen < 60:
                await self.mqtt.publish(self.base + "/availability", "online")
            await asyncio.sleep(15)

    async def control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            obj = json.loads(await asyncio.wait_for(reader.readline(), 5))
            action = obj.get("action")
            result: JsonObject | None
            if action == "health":
                result = {
                    "online": self.online,
                    "connected": self.conn is not None,
                    "mqtt_connected": self.mqtt.connected.is_set(),
                    "properties": self.properties,
                    "state": state_from_properties(
                        self.properties, self.current_temperature
                    ),
                    "last_error": self.last_error,
                }
            elif action == "set":
                await self.set_properties(
                    commands_for(obj["field"], obj["value"], self.properties)
                )
                result = {
                    "ok": True,
                    "state": state_from_properties(
                        self.properties, self.current_temperature
                    ),
                }
            elif action == "query":
                await self.query()
                result = {"ok": True, "properties": self.properties}
            elif action == "raw":
                result = await self.command(
                    obj["inner"], obj.get("target"), obj.get("index")
                )
            else:
                raise ValueError("Unknown action")
            writer.write(json.dumps(result).encode() + b"\n")
        except Exception as exc:
            writer.write(json.dumps({"error": str(exc)}).encode() + b"\n")
        await writer.drain()
        writer.close()


async def run(config: JsonObject) -> None:
    gateway = Gateway(config)
    dns_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: DeviceDNS(
            config["device_ip"],
            config["external_ip"],
            config.get("dns_names", ["eu.dis.gree.com", "mqtt-eu.gree.com"]),
        ),
        local_addr=("0.0.0.0", 1053),
    )
    server = await asyncio.start_server(
        gateway.handle,
        config.get("listen_host", "0.0.0.0"),
        config.get("listen_port", 1982),
    )
    control = await asyncio.start_server(gateway.control, "127.0.0.1", 1983)
    dispatcher = await asyncio.start_server(
        gateway.dispatcher, config.get("listen_host", "0.0.0.0"), 1813
    )
    tasks = [
        asyncio.create_task(gateway.mqtt.run()),
        asyncio.create_task(gateway.poll()),
        asyncio.create_task(gateway.refresh_from_push()),
        asyncio.create_task(gateway.heartbeat()),
    ]
    _LOG.info(
        "Local Gree endpoint listening on %s; control on localhost:1983",
        config.get("listen_port", 1982),
    )
    try:
        async with server, control, dispatcher:
            await server.serve_forever()
    finally:
        await gateway.set_online(False)
        dns_transport.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def load_config() -> JsonObject:
    certificates = Path(__file__).resolve().parents[2] / "certs"
    config: JsonObject = {
        "name": "Gree AC",
        "model": "U-WB04BRT13",
        "listen_host": "0.0.0.0",
        "listen_port": 1982,
        "poll_interval": 20,
        "private_dir": "/data",
        "certificate": str(certificates / "gree-device.pem"),
        "dispatcher_certificate": str(certificates / "gree-dispatcher.pem"),
        "dns_names": ["eu.dis.gree.com", "mqtt-eu.gree.com"],
    }
    for env, key in [
        ("GREE_SERVER_IP", "external_ip"),
        ("GREE_DEVICE_IP", "device_ip"),
        ("GREE_MODULE_MAC", "module_mac"),
        ("GREE_UNIT_MAC", "unit_mac"),
        ("GREE_NAME", "name"),
        ("GREE_MODEL", "model"),
        ("GREE_DATA_DIR", "private_dir"),
        ("GREE_CERTIFICATE", "certificate"),
        ("GREE_DISPATCHER_CERTIFICATE", "dispatcher_certificate"),
    ]:
        if os.environ.get(env):
            config[key] = os.environ[env]
    for env, key in [
        ("GREE_LISTEN_PORT", "listen_port"),
        ("GREE_POLL_INTERVAL", "poll_interval"),
    ]:
        if os.environ.get(env):
            config[key] = int(os.environ[env])
    if os.environ.get("GREE_DNS_NAMES"):
        config["dns_names"] = [
            name.strip()
            for name in os.environ["GREE_DNS_NAMES"].split(",")
            if name.strip()
        ]
    for key in ("device_ip", "external_ip", "module_mac", "unit_mac"):
        if not config.get(key):
            raise ValueError(
                f"Missing {key}; set the corresponding GREE_* environment variable"
            )
    if not 1 <= config["listen_port"] <= 65535:
        raise ValueError("GREE_LISTEN_PORT must be between 1 and 65535")
    if config["poll_interval"] <= 0:
        raise ValueError("GREE_POLL_INTERVAL must be positive")
    config["language"] = os.environ.get(
        "LANGUAGE", config.get("language", "en")
    ).lower()
    if config["language"] not in ("en", "it"):
        raise ValueError("LANGUAGE / language must be en or it")
    broker = config.setdefault("mqtt", {})
    for env, key in [
        ("MQTT_HOST", "host"),
        ("MQTT_USERNAME", "username"),
        ("MQTT_PASSWORD", "password"),
        ("MQTT_PASSWORD_FILE", "password_file"),
        ("MQTT_CLIENT_ID", "client_id"),
        ("MQTT_CA_FILE", "ca_file"),
    ]:
        if env in os.environ:
            broker[key] = os.environ[env]
    if os.environ.get("MQTT_PORT"):
        broker["port"] = int(os.environ["MQTT_PORT"])
    for env, target, key in [
        ("MQTT_TLS", broker, "tls"),
        ("HA_DISCOVERY", config, "ha_discovery"),
    ]:
        if env in os.environ:
            value = os.environ[env].lower()
            if value not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                raise ValueError(f"{env} must be true or false")
            target[key] = value in ("true", "1", "yes", "on")
    if "TEMPERATURE_TOPIC" in os.environ:
        config["temperature_topic"] = os.environ["TEMPERATURE_TOPIC"] or None
    if not broker.get("host"):
        raise ValueError("Set MQTT_HOST to your broker hostname")
    if not 1 <= broker.get("port", 1883) <= 65535:
        raise ValueError("MQTT port must be between 1 and 65535")
    private = Path(config.get("private_dir", "/data"))
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials_file = private / "mqtt-credentials.json"
    if not config.get("mqtt_username") or not config.get("mqtt_password"):
        if credentials_file.exists():
            credentials = json.loads(credentials_file.read_text())
        else:
            credentials = {
                "mqtt_username": secrets.token_hex(8),
                "mqtt_password": secrets.token_hex(8),
            }
            # Exclusive creation avoids accidentally replacing credentials on restart.
            with open(
                credentials_file,
                "x",
                opener=lambda name, flags: os.open(name, flags, 0o600),
            ) as stream:
                json.dump(credentials, stream)
        config.update(credentials)
    for field in ["external_ip", "device_ip"]:
        import ipaddress

        address = ipaddress.IPv4Address(config[field])
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError(f"{field} must be a usable LAN address")
    return config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(run(load_config()))


if __name__ == "__main__":
    main()
