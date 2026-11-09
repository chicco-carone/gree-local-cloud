# Find your AC’s settings

Collect these before filling in `.env`. Pair the AC to your Wi-Fi using its normal app first. This bridge does not perform Wi-Fi pairing.

## 1. IP address and Wi-Fi MAC

Open your router’s connected-client or DHCP lease list and find the AC.

| Router value | Bridge setting |
| --- | --- |
| AC IPv4 address | `GREE_DEVICE_IP` |
| Docker host IPv4 address | `GREE_SERVER_IP` |
| Wi-Fi MAC, e.g. `aa:bb:cc:dd:ee:ff` | Module-ID candidate `aabbccddeeff` |

The Wi-Fi module ID normally matches its Wi-Fi MAC without colons. Confirm it against the parent module reported by the cloud. The indoor-unit ID is separate and does not appear in the router’s client list.

## 2. Module and indoor-unit IDs

### Without Home Assistant

From the repository root, run the optional read-only helper:

```sh
uv run tools/device_info.py --region Europe
```

Use the same account and region as your Gree app. The helper prompts for the username and password, lists devices from the account, and closes the session. It uses the separate [greeclimate library](https://github.com/davo22/greeclimate) for this one-time lookup; the bridge itself does not need those cloud credentials.

The pinned library supports these region names: `Europe`, `North American`, `South American`, `East South Asia`, `China Mainland`, `India`, `Middle East`, `Australia`, and `Russia`.

For a child indoor-unit record, output includes:

```dotenv
GREE_MODULE_MAC=aabbccddeeff
GREE_UNIT_MAC=aabbccddeeff00
```

Copy the values printed for your intended indoor unit. The helper derives the module ID from the record’s parent (`pmac`) and the unit ID from its own `mac`; it does **not** guess a suffix. A module record may also appear separately. If a record has no parent, the helper shows its details without generating that pair of settings. Do not infer support or invent an indoor-unit ID from that record alone.

### With an existing Gree Cloud integration

If you already use [Gree Cloud for Home Assistant](https://github.com/davo22/homeassistant-gree-cloud), you can inspect its debug logs instead:

1. Open **Settings → Devices & services → Gree Cloud** and enable debug logging.
2. Reload the integration while the AC is still reachable through the cloud.
3. Disable debug logging and download the log.
4. Find the relevant `Cloud device record` entry by its device name. If your integration/library version does not log that record, use the helper above.

In a child record, `pmac` is the Wi-Fi module ID (`GREE_MODULE_MAC`) and `mac` is the indoor-unit ID (`GREE_UNIT_MAC`). Remove colons and use lowercase hexadecimal. These fields are handled by the library’s [device-list parser](https://github.com/davo22/greeclimate/blob/8ec8f5c461152d0b41731b1ad5197e43eb36d10b/greeclimate_davo22/cloud_api.py).

## 3. Module model and firmware

Look at the Gree app’s device-information/firmware screen and, where accessible without opening electrical equipment, the Wi-Fi module’s label. Record the module model (tested: `U-WB04BRT13`) and its actual firmware version (tested: `1.38` and `1.40`). Menu names differ between Gree and rebranded apps.

The AC’s product model, the wall-controller model, the Wi-Fi module model, and its firmware are different pieces of information. The cloud `ver` field printed by the helper can describe a protocol/software version; do not assume it is the firmware version used in this project’s compatibility table. If the app or label does not identify the module/firmware, report that as unknown.

## 4. Copy into `.env`

```dotenv
GREE_SERVER_IP=192.0.2.10
GREE_DEVICE_IP=192.0.2.20
GREE_MODULE_MAC=aabbccddeeff
GREE_UNIT_MAC=aabbccddeeff00
GREE_NAME=Living room AC
```

Replace the documentation addresses and identifiers with your own. `GREE_NAME` is simply your chosen display name. Read the [setup instructions](../README.md#setup), then apply the router redirection. Keep full logs and cloud records private: they can contain account information or device secrets.
