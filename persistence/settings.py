"""Centralized persistence settings validated from the central policy section.

Enforces strict type and value constraints: rejects booleans for numbers,
rejects unknown keys, non-finite or negative values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Dict


@dataclass(frozen=True)
class PersistenceSettings:
    """Store-wide and job-level capacity, lifecycle, and export limits."""

    outbox_maxsize: int = 10000
    max_retries: int = 3
    timeout_seconds: float = 5.0
    lease_seconds: float = 35.0
    batch_size: int = 50
    poll_timeout: float = 0.05
    max_export_rows: int = 10000
    max_export_bytes: int = 8388608  # 8 MiB
    export_hold_seconds: float = 300.0
    terminal_retention_seconds: float = 86400.0  # 24 hours
    maintenance_interval_seconds: float = 60.0
    max_retained_logical_bytes: int = 67108864  # 64 MiB
    min_free_bytes: int = 8388608  # 8 MiB

    def __post_init__(self) -> None:
        # Validate all fields strictly
        int_fields = {
            "outbox_maxsize": (1, 1_000_000),
            "max_retries": (0, 100),
            "batch_size": (1, 10_000),
            "max_export_rows": (1, 100_000),
            "max_export_bytes": (1024, 1_073_741_824),
            "max_retained_logical_bytes": (1024, 1_073_741_824),
            "min_free_bytes": (0, 1_073_741_824),
        }
        float_fields = {
            "timeout_seconds": (0.001, 3600.0),
            "lease_seconds": (1.0, 86400.0),
            "poll_timeout": (0.001, 60.0),
            "export_hold_seconds": (1.0, 86400.0),
            "terminal_retention_seconds": (1.0, 31_536_000.0),
            "maintenance_interval_seconds": (1.0, 86400.0),
        }

        for name, (low, high) in int_fields.items():
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, int) or not (low <= val <= high):
                raise ValueError(
                    f"Setting '{name}' must be an integer between {low} and {high}, got {val!r}"
                )

        for name, (low, high) in float_fields.items():
            val = getattr(self, name)
            if (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or not math.isfinite(val)
                or not (low <= val <= high)
            ):
                raise ValueError(
                    f"Setting '{name}' must be a finite number between {low} and {high}, got {val!r}"
                )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize settings to a plain dict."""
        return asdict(self)

    @classmethod
    def from_policy(cls, section: Dict[str, Any]) -> PersistenceSettings:
        """Validate and construct PersistenceSettings from the central policy section.

        Rejects unknown keys, booleans for numbers, and invalid types.
        """
        if not isinstance(section, dict):
            raise ValueError(f"Persistence policy section must be a dict, got {type(section).__name__}")

        allowed_keys = set(cls.__dataclass_fields__.keys())
        unknown = set(section.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"Unknown persistence policy settings: {sorted(unknown)}")

        # Construct instance (validation happens in __post_init__)
        return cls(**section)
