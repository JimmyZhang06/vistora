from __future__ import annotations

import os
from pathlib import Path


def environment_value(name: str) -> str | None:
    """Read a setting directly or from its Docker/Kubernetes ``_FILE`` secret."""
    direct = os.getenv(name)
    secret_file = os.getenv(f"{name}_FILE")
    if direct and secret_file:
        raise ValueError(f"set only one of {name} and {name}_FILE")
    if direct is not None:
        value = direct.strip()
        return value or None
    if not secret_file:
        return None
    try:
        value = Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read {name}_FILE") from exc
    if not value:
        raise ValueError(f"{name}_FILE is empty")
    return value
