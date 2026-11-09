# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "greeclimate-davo22 @ git+https://github.com/davo22/greeclimate.git@8ec8f5c461152d0b41731b1ad5197e43eb36d10b",
# ]
# ///
"""Read device identifiers from a Gree account without sending AC commands."""

from __future__ import annotations

import argparse
import asyncio
from getpass import getpass
from typing import Protocol


class DeviceInfo(Protocol):
    name: str
    mac: str
    parent_mac: str | None
    model: str | None
    version: str | None


def display_device(device: DeviceInfo) -> None:
    print(f"\nDevice: {device.name}")
    print(f"Record MAC: {device.mac}")
    print(f"Parent module: {device.parent_mac or '(not provided)'}")
    print(f"Model: {device.model or '(not provided)'}")
    print(f"Cloud version field: {device.version or '(not provided)'}")
    if device.parent_mac:
        module = device.parent_mac.replace(":", "").lower()
        unit = device.mac.replace(":", "").lower()
        print(f"GREE_MODULE_MAC={module}")
        print(f"GREE_UNIT_MAC={unit}")


async def list_devices(region: str, username: str, password: str) -> None:
    from greeclimate_davo22.cloud_api import GreeCloudApi  # ty: ignore[import-not-found] # Installed by uv script metadata.

    async with GreeCloudApi.for_server(region, username, password) as api:
        await api.login()
        found = False
        for home in await api.get_homes():
            for device in await api.get_devices(home.id):
                found = True
                display_device(device)
        if not found:
            print("No devices found. Check your account and cloud region.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region", default="Europe", help="Gree account region (default: Europe)"
    )
    args = parser.parse_args()
    username = input("Gree account email/username: ").strip()
    password = getpass("Gree account password: ")
    asyncio.run(list_devices(args.region, username, password))


if __name__ == "__main__":
    main()
