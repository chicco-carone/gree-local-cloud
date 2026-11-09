"""Docker readiness probe; no authentication data is returned."""

from __future__ import annotations

import json
import socket


def main() -> int:
    try:
        with socket.create_connection(("127.0.0.1", 1983), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(b'{"action":"health"}\n')
            data = bytearray()
            while b"\n" not in data and len(data) < 65536:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data.extend(chunk)
            health = json.loads(data)
        return 0 if health.get("online") and health.get("mqtt_connected") else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
