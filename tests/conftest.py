from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def bridge_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "GREE_SERVER_IP": "192.0.2.10",
        "GREE_DEVICE_IP": "192.0.2.20",
        "GREE_MODULE_MAC": "aabbccddeeff",
        "GREE_UNIT_MAC": "aabbccddeeff00",
        "GREE_DATA_DIR": str(tmp_path / "data"),
        "MQTT_HOST": "localhost",
    }.items():
        monkeypatch.setenv(name, value)
