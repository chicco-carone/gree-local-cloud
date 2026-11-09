"""Shared types for dynamic JSON messages and numeric device properties."""

from typing import Any

# Configuration and vendor JSON contain heterogeneous, firmware-dependent fields.
type JsonObject = dict[str, Any]
type Properties = dict[str, int]
