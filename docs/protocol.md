# Protocol and compatibility notes

Verified on U-WB04BRT13 modules with firmware 1.38 and 1.40. Other module families, firmware, regions, indoor units, and multi-unit arrangements are unverified.

The device uses a TLS dispatcher (TCP 1813) to obtain a MQTT 3.1 endpoint (observed cloud ports 1982/1992). It validates Gree certificates. This implementation derives predictable TLS randomness on the tested firmware and completes the handshake using the public Gree certificates in `certs/`, without a vendor private key or device firmware modification. Firmware with corrected randomness will not work with this method. This is a device-specific compatibility implementation, not a general TLS server.

The module and indoor unit have separate identifiers. Device messages use `request/<module>`, `response/<module>/…`, and `binStatus/<module>/<unit>` topics. Local binding obtains an AES device key; generated MQTT credentials and that key persist in the data volume.

Commands are confirmed by subsequent device status readback, not just acknowledgements. On the tested controller, sending `SetTem` and `SetDeciTem` together truncates half-degree values. Decimal setpoints therefore use `SetDeciTem` alone. Binary notifications trigger a coalesced status read with a 50 ms debounce; a periodic poll remains as a fallback.

Tests performed during initial development included temperature, fan speed, both swing directions, eco mode, fan-only power, restart recovery, and operation with Internet access blocked. An external circuit meter confirmed physical fan operation. These results apply to the tested units; exposed controls are not automatically discovered from each model’s capabilities.

References:

- https://github.com/tomikaa87/gree-remote
- https://github.com/davo22/homeassistant-gree-cloud
- https://github.com/maxim-smirnov/gree-wifimodule-firmware

`research/firmware_xrefs.py` is an optional firmware analysis utility. It additionally requires Capstone and a separately obtained firmware dump at `.private/firmware-1.38.bin`. It is not part of the runtime or normal installation.

## MQTT transport

Version 0.2.0 connects directly to a user-configured MQTT broker. Home Assistant discovery is optional (`HA_DISCOVERY=false` disables it). Broker last will and graceful shutdown publish retained offline availability; an external room temperature can be supplied as a numeric MQTT message. The earlier HA WebSocket API transport and its watchdog are no longer used. This change does not alter the AC-facing protocol.
