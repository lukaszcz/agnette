"""Shared helpers for tests that build ``GeneralConfig`` views by hand."""

from __future__ import annotations

from collections.abc import Mapping

from agnette.config.general import GeneralConfig
from agnette.core.toml import toml_dict


def unanchored_config(*layers: Mapping[str, object]) -> GeneralConfig:
    """Return a config of *layers*, lowest precedence first, whose path values stay as written."""
    return GeneralConfig.from_layers((toml_dict(layer) for layer in layers), (None,) * len(layers))
