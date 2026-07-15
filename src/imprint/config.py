"""Portable public configuration. Private runtime state is never packaged."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .paths import default_data_root, operator_root, validate_data_root

DEFAULTS = {
    "config_version": "3.0.0",
    "operator_slug": "default",
    "node_id": "primary",
    "compiler": True,
    "context_budget_bytes": 32768,
    "allow_higher_budget": False,
    "spool_retention_days": 30,
    "domains": [],
    "experimental": {"digest": False, "profile_learning": False},
}

# Keys the loader recognizes. ``data_root`` and ``hooks_dir`` are written by the
# installers but are not part of the portable defaults. Unknown keys are rejected
# unless namespaced with a dot, which reserves an extension space the loader
# preserves but does not interpret.
KNOWN_TOP_LEVEL = frozenset(DEFAULTS) | {"data_root", "hooks_dir"}


def config_path() -> Path:
    override = os.environ.get("IMPRINT_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "Imprint" / "config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "imprint" / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    data = dict(DEFAULTS)
    if target.exists():
        try:
            loaded = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"corrupt config: {target}") from exc
        if not isinstance(loaded, dict):
            raise ValidationError("config must be an object")
        unknown = {key for key in loaded if key not in KNOWN_TOP_LEVEL and "." not in key}
        if unknown:
            raise ValidationError(
                f"unknown config keys: {sorted(unknown)}; namespace extensions with a dot"
            )
        data.update(loaded)
    if data.get("config_version") != DEFAULTS["config_version"]:
        raise ValidationError("unsupported config_version")
    if not isinstance(data.get("context_budget_bytes"), int) or not 4096 <= data["context_budget_bytes"] <= 131072:
        raise ValidationError("context_budget_bytes must be 4096..131072")
    if data["context_budget_bytes"] > 32768 and data.get("allow_higher_budget") is not True:
        raise ValidationError("context_budget_bytes above 32768 requires allow_higher_budget=true")
    if not isinstance(data.get("spool_retention_days"), int) or not 1 <= data["spool_retention_days"] <= 36500:
        raise ValidationError("spool_retention_days must be 1..36500")
    try:
        from .domains import registry_from_config
        registry_from_config(data)
    except ValueError as exc:
        raise ValidationError(f"invalid domains config: {exc}") from exc
    return data


def resolved_operator_root(config: dict[str, Any]) -> Path:
    explicit = config.get("data_root")
    base = validate_data_root(Path(explicit).expanduser()) if explicit else default_data_root()
    return operator_root(str(config["operator_slug"]), base)
