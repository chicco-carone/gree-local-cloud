# Configuration and usage reference

Run the commands below from the repository root. For a shorter introduction, see the [README](../README.md).

## Configure and start

Clone the repository and prepare the configuration:

```sh
git clone https://github.com/chicco-carone/gree-local-cloud.git
cd gree-local-cloud
cp .env.example .env
mkdir -p data
```

Edit `.env`:

```dotenv
GREE_IMAGE=ghcr.io/chicco-carone/gree-local-cloud:latest
GREE_SERVER_IP=192.0.2.10
GREE_DEVICE_IP=192.0.2.20
GREE_MODULE_MAC=aabbccddeeff
GREE_UNIT_MAC=aabbccddeeff00
GREE_NAME=Living room AC
MQTT_HOST=mqtt.example.local
MQTT_PORT=1883
MQTT_USERNAME=gree
MQTT_PASSWORD=change-me
MQTT_TLS=false
HA_DISCOVERY=true
LANGUAGE=en
TEMPERATURE_TOPIC=
GREE_UID=1000
GREE_GID=1000
TZ=UTC
```

The example `192.0.2.x` addresses throughout this project are documentation placeholders: replace them with your actual LAN addresses. `GREE_SERVER_IP` is your Docker host, not the registry or Home Assistant host. Set `MQTT_HOST` to your broker’s reachable hostname or IP. For TLS use `MQTT_TLS=true` and usually `MQTT_PORT=8883`; the broker certificate is verified using the system trust store. Keep `.env` private. Quote values containing special characters using Compose’s `.env` syntax (single quotes preserve literal `$` and `#`).

All application settings are supplied through the Compose `environment` section, using `.env` values. No JSON configuration file is needed or supported.

| Variable | What to set |
| --- | --- |
| `GREE_DEVICE_IP` | AC’s reserved IPv4 address; incoming connections are restricted to it. |
| `GREE_MODULE_MAC` | Wi-Fi module identifier, without colons. |
| `GREE_UNIT_MAC` | Indoor-unit identifier; often the module identifier followed by `00`, but verify it. |
| `GREE_NAME` | Display name in Home Assistant. |
| `GREE_MODEL` | Descriptive model text; defaults to `U-WB04BRT13`. |
| `GREE_LISTEN_PORT` | Device MQTT TCP port; defaults to `1982` and is advertised by the dispatcher. |
| `GREE_POLL_INTERVAL` | Fallback status polling interval in seconds; defaults to `20`. |
| `GREE_DNS_NAMES` | Comma-separated Gree hostnames; defaults to `eu.dis.gree.com,mqtt-eu.gree.com`. |
| `TEMPERATURE_TOPIC` | Optional MQTT topic with numeric room temperature in °C. |

Follow [Find your AC’s settings](device-info.md) for router IP/MAC lookup, a read-only cloud-account helper, and the `pmac`/`mac` mapping from existing integration logs. Do not guess the indoor-unit suffix or confuse the cloud version field with firmware.

Certificates are included in the image, and runtime data is stored in `/data`. Advanced environment settings are `GREE_DATA_DIR`, `GREE_CERTIFICATE`, and `GREE_DISPATCHER_CERTIFICATE`; add them to Compose only if overriding the container defaults.

For file-based passwords, `MQTT_PASSWORD_FILE` takes precedence over `MQTT_PASSWORD`; mount the file read-only and pass its container path using a Compose override. A custom broker CA is supported via `MQTT_CA_FILE`; mount that file too. Neither setting is required for a basic username/password connection. Device-side MQTT credentials are generated internally and are separate from your broker credentials.

The image is published at [`ghcr.io/chicco-carone/gree-local-cloud`](https://github.com/chicco-carone/gree-local-cloud/pkgs/container/gree-local-cloud). While the repository and package are private, you need access and an authenticated Docker login to GHCR to pull it.

Start the server before redirecting any traffic:

```sh
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f gree-local
```

The container runs as an unprivileged user with a persistent data volume. Compose enables automatic restarts and uses Docker’s default logging configuration. Device keys, generated device MQTT credentials, and decoded protocol logs are stored in `data/`; keep that directory private and back it up. The health check requires both a live AC session and a broker connection. Check logs and the MQTT availability topic (or HA entity) after routing is configured.

## Router configuration

Configure the following on your router for **each AC**. Use your own reserved device IP, Docker host IP, and published host ports. The router brand and VLAN numbers do not matter, but the AC’s outgoing traffic must traverse the router for these rules to apply.

| Rule | Match | Translate / action |
| --- | --- | --- |
| DNS redirect | AC source IP, UDP destination port 53 | Docker host, UDP 1053 |
| Dispatcher redirect | AC source IP, TCP destination port 1813 | Docker host, TCP 1813 |
| MQTT redirect | AC source IP, TCP destination port 1982 | Docker host, TCP 1982 |
| Alternate MQTT redirect | AC source IP, TCP destination port 1992 | Docker host, TCP 1982 |
| Local access | AC → Docker host on the translated ports | Allow, including established return traffic |
| Internet isolation | AC → Internet | Block IPv4 and IPv6; keep local bridge access allowed |

Use destination NAT on the interface receiving traffic from the AC’s network. Restrict each translation to that AC’s source IP and preserve its source address. Place its DNS redirect before broader DNS interception rules. Keep existing rules for other clients.

These are LAN-originated redirects. A WAN port-forward does not provide the required behavior. No static route to a Gree cloud IP is needed. DNS answers alone cannot change ports or catch already-known cloud addresses.

The bundled device-scoped DNS service answers `eu.dis.gree.com` and `mqtt-eu.gree.com`. It has no upstream resolver; other regions may need different `GREE_DNS_NAMES`. Avoid a network-wide Gree DNS override when different ACs need different container ports. Keep AC and Docker host IP reservations stable.

For UniFi, follow [the illustrated UniFi configuration guide](router-unifi.md). The router setup was tested on a **Cloud Gateway Ultra** and should also work on other UniFi gateways supporting these NAT and firewall policies; settings may be located or named differently depending on the model and software version. For gateways requiring shell rules, the [optional iptables instructions](router-iptables.md) provide an alternative.

Start the container before enabling redirects, reconnect the AC if it keeps an old connection open, and verify MQTT availability and a device status read. For rollback, disable only this AC’s native policies and reconnect it to restore cloud access. Do not remove shared DNS or firewall policies used by other devices.

## MQTT interface (with or without Home Assistant)

Set `HA_DISCOVERY=false` in `.env` and recreate the container to disable HA discovery publications and the `homeassistant/status` subscription. State, commands, availability, and the optional temperature subscription continue to work. The default is `true`.

For a configured unit ID `aabbccddeeff00`, the base topic is `gree_local/aabbccddeeff00`:

| Topic suffix | Direction | Payload |
| --- | --- | --- |
| `/state` | Bridge → broker, retained | JSON with mode, temperature, fan_mode, preset_mode, and swing values. |
| `/raw` | Bridge → broker, retained | JSON of raw Gree properties. |
| `/availability` | Bridge → broker, retained | `online` or `offline`; broker last will marks unexpected disconnects offline. |
| `/room_temperature` | Bridge → broker, retained | Numeric °C when supplied by an external temperature topic. |
| `/heartbeat` | Bridge → broker | Unix timestamp, every 15 seconds. |
| `/last_command` | Bridge → broker | JSON confirmation or error; not retained. |
| `/command/<field>` | Broker → bridge | Plain text command; publish **without retain**. |

Supported command fields and values:

- `mode`: `off`, `auto`, `cool`, `dry`, `fan_only`, `heat`.
- `temperature`: numeric Celsius, for example `23.5`.
- `fan_mode`: `auto`, `low`, `medium-low`, `medium`, `medium-high`, `high`.
- `preset_mode`: `none`, `eco`, `away`, `boost`, `sleep`.
- `swing_mode`: `default`, `full_swing`, `fixed_upper`, `fixed_upper_middle`, `fixed_middle`, `fixed_lower_middle`, `fixed_lower`.
- `swing_horizontal_mode`: `default`, `full_swing`, `left`, `left_center`, `center`, `right_center`, `right`.
- `light`, `quiet`, `xfan`, `health`, `fresh_air`: `ON` or `OFF`.

Subscribe to `gree_local/<unit_mac>/#` in any MQTT client to inspect state and command confirmations. Use a broker account with read access to command topics and your optional temperature topic, and write access to the bridge’s topics. When discovery is enabled, also allow writes under `homeassistant/` and reads of `homeassistant/status`.

`MQTT_CLIENT_ID` can override the default `gree-local-<unit_mac>` identifier; every running instance needs a unique client ID. Broker connections automatically reconnect and resubscribe.

To use an external room sensor, set `TEMPERATURE_TOPIC` to a topic with plain numeric Celsius payloads, preferably retained so the reading is available on reconnect. JSON sensor payloads require a separate MQTT transformation. There is no HA entity lookup.

## Optional Home Assistant discovery

MQTT Discovery creates a climate entity and additional switches for panel light, quiet mode, XFan, health, and fresh air. Modes, presets, swing positions, and switches depend on the indoor unit; exposing a control does not prove every model implements it. Temperature commands support 8–30°C, with half-degree values only when reported by the device.

Entity IDs are assigned by Home Assistant from your chosen device name. To replace an existing cloud entity in dashboards and automations, rename the old entity first, then assign its former entity ID to the new MQTT entity in HA. Discovery identifiers remain tied to `unit_mac`.

Some modules do not report room temperature. Set `TEMPERATURE_TOPIC` to a separate room sensor’s numeric MQTT topic in that case; otherwise current temperature may remain unavailable. Set `LANGUAGE=en` (default) or `LANGUAGE=it` in `.env` to select English or Italian swing menus. Recreate the container to republish discovery after changing the setting; existing entity IDs are preserved. This selects one menu language per bridge/device for all HA users; it does not follow each user’s HA interface language. MQTT command and state values remain canonical English identifiers in either language.

Physical-controller notifications trigger immediate coalesced status reads, with periodic polling as a fallback. Command acknowledgements are followed by a status read to confirm the setting.

The bridge now supplies a retained broker last will and publishes offline on graceful shutdown. The previous HA availability watchdog is no longer required.

If discovery was previously enabled, switching it off does not remove retained discovery messages already stored on the broker. Remove the old MQTT device in HA and clear its retained discovery configuration topics if you want it to disappear. This toggle deliberately makes no HA discovery publications when disabled.

### Migrating from the HA API transport

Replace the old Compose file with this version, configure your broker credentials, and recreate the container. Keep the device data and set the same module/unit identifiers in `.env` to preserve device binding and discovery identity. Transfer any old JSON settings into the environment variables above and remove the old Compose `command` override; `--config` is no longer supported. Replace an external HA temperature entity with `TEMPERATURE_TOPIC`, and disable the old HA heartbeat watchdog. Router redirection does not change. These source changes do not update any already-running deployment automatically.

## Multiple ACs

Run one service per AC with separate environment settings and a data volume, unique module and unit IDs, and distinct host ports. Broker credentials can be shared if your broker permissions allow it.

For example, a second AC can use DNS port `1054`, dispatcher port `1814`, and MQTT port `1984`. Keep container DNS/dispatcher ports at `1053/1813`, but set the second service’s `GREE_LISTEN_PORT` to `1984` and publish `1984:1984`. The dispatcher advertises this MQTT port directly. Add native router policies with the second device’s IP/MAC and matching host ports. Every instance needs a distinct MQTT client ID (the default derives it from the unit ID).

## Troubleshooting

- **Broker connection errors:** check `MQTT_HOST`, port, username/password, TLS trust, broker permissions, and container logs.
- **No device connection:** check device IP/MAC, source-IP preservation, routing/firewall rules, container ports, and existing cloud connections.
- **TLS handshake errors:** confirm the exact module and firmware version. Changing DNS alone does not make unsupported firmware compatible.
- **Duplicate entities:** check the old integration and HA entity registry; do not change `unit_mac` to rename an entity.
- **Unavailable room temperature:** configure an external sensor; not all modules supply a reading.

Keep device-facing services on a trusted LAN and restrict access to the configured AC. Broker passwords, device keys, and decoded logs are sensitive. Do not include `data/`, `secrets/`, `.env`, captures, or private firmware dumps in issues or image layers.

## Development

Python 3.12+ and uv are required:

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format .
uv run ty check
```

Tests cover TLS compatibility, authenticated records and corrupt-record rejection, RNG recovery, MQTT framing, temperature encoding, command readback, configuration persistence, swing label mapping, and notification-driven refresh. They do not send commands to real equipment.

Runtime code is in `src/gree_local/`, public protocol certificates in `certs/`, deployment examples in `deploy/`, and protocol findings in [docs/protocol.md](protocol.md). Public certificates contain no vendor private keys. Private investigation artifacts are excluded from Git and the Docker build context.

Type annotations are required on all functions by Ruff, and `ty` checks the runtime, tests, lookup helper, and research code. CI runs both checks. The optional cloud-lookup and firmware-analysis dependencies are resolved separately; only their missing-import diagnostics are suppressed at those imports.
