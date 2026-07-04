"""Shared run-configuration handling for CLI and Web-IDE.

A *run configuration* is a named bundle of flags + env vars that customize
``raccoon run``. It works like a PyCharm/IntelliJ run configuration: the
user picks a name (``raccoon run dev``) and that resolves to a fixed set of
``--no-calibrate``, ``--no-checkpoints``, ``--dev`` etc. plus any custom
environment variables.

Configurations live in ``raccoon.project.yml`` under the top-level
``run_configurations:`` key. Builtin presets (``default``, ``dev``) are
returned even when the file has none, so the IDE always has *something* to
show in its dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from raccoon_cli.project import (
    ProjectError,
    load_project_config,
    save_project_keys,
)


CONFIG_KEY = "run_configurations"
HIDDEN_KEY = "hidden_run_configurations"


@dataclass
class RunConfiguration:
    """A named run configuration.

    Attributes mirror the CLI flags of ``raccoon run`` plus an ``env`` map
    of environment variables forwarded to the child process. ``target``
    chooses between local execution, the remote Pi, or the libstp
    simulator — same semantics as the IDE's existing ``runTarget`` toggle.
    """

    name: str
    description: str = ""
    target: str = "auto"  # "auto" | "local" | "remote" | "simulated"
    dev: bool = False
    no_calibrate: bool = False
    no_checkpoints: bool = False
    debug: bool = False
    no_codegen: bool = False
    no_sync: bool = False
    record_localization: bool = True
    profile: bool = True
    record_hz: Optional[float] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    builtin: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Builtins are derived — never persist them.
        d.pop("builtin", None)
        return d

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "RunConfiguration":
        if not isinstance(data, dict):
            raise ProjectError(
                f"Run configuration '{name}' must be a mapping, got {type(data).__name__}"
            )
        allowed = {
            "description", "target", "dev", "no_calibrate", "no_checkpoints",
            "debug", "no_codegen", "no_sync", "record_localization", "profile",
            "record_hz", "args", "env",
        }
        kwargs: Dict[str, Any] = {"name": name}
        for key, value in data.items():
            if key not in allowed:
                continue
            kwargs[key] = value
        # Light validation for the most common foot-guns.
        if "target" in kwargs and kwargs["target"] not in {
            "auto", "local", "remote", "simulated",
        }:
            raise ProjectError(
                f"Run configuration '{name}': target must be one of "
                "auto/local/remote/simulated"
            )
        if "args" in kwargs and not isinstance(kwargs["args"], list):
            raise ProjectError(f"Run configuration '{name}': args must be a list")
        if "env" in kwargs:
            env_val = kwargs["env"]
            if not isinstance(env_val, dict):
                raise ProjectError(f"Run configuration '{name}': env must be a mapping")
            kwargs["env"] = {str(k): str(v) for k, v in env_val.items()}
        return cls(**kwargs)


# --- Builtin presets -------------------------------------------------------

def _builtin_presets() -> Dict[str, RunConfiguration]:
    """Always-available configurations, surfaced even with an empty config."""
    return {
        "default": RunConfiguration(
            name="default",
            description="Standard run (codegen + calibrate + checkpoints)",
            builtin=True,
        ),
        "dev": RunConfiguration(
            name="dev",
            description="Fast iteration: --dev --no-calibrate --no-checkpoints",
            dev=True,
            no_calibrate=True,
            no_checkpoints=True,
            builtin=True,
        ),
        "simulated": RunConfiguration(
            name="simulated",
            description="Run under the libstp simulator",
            target="simulated",
            builtin=True,
        ),
    }


# --- Load / save -----------------------------------------------------------

def load_run_configurations(
    project_root: Path,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, RunConfiguration]:
    """Return the merged set of {name → RunConfiguration} for *project_root*.

    Builtins are always present and may be overridden by user entries with
    the same name.  ``config`` is optional and used to avoid a duplicate
    ``raccoon.project.yml`` read when the caller already has it.
    """
    if config is None:
        config = load_project_config(project_root)

    raw = config.get(CONFIG_KEY) or {}
    if not isinstance(raw, dict):
        raise ProjectError(
            f"raccoon.project.yml: '{CONFIG_KEY}' must be a mapping of name → "
            "configuration"
        )

    hidden_raw = config.get(HIDDEN_KEY) or []
    if not isinstance(hidden_raw, list):
        raise ProjectError(
            f"raccoon.project.yml: '{HIDDEN_KEY}' must be a list of names"
        )
    hidden = {str(name) for name in hidden_raw}

    result = _builtin_presets()
    # Drop tombstoned builtins so neither the CLI nor the IDE sees them.
    # A user entry with the same name re-introduces it below.
    for name in list(result):
        if name in hidden:
            result.pop(name)

    for name, data in raw.items():
        rc = RunConfiguration.from_dict(str(name), data or {})
        # User-defined entries shadow the builtin presets but keep the
        # builtin flag off so the IDE knows they're editable.
        rc.builtin = False
        result[rc.name] = rc
    return result


def get_run_configuration(
    project_root: Path,
    name: str,
    config: Optional[Dict[str, Any]] = None,
) -> RunConfiguration:
    """Look up a single run configuration by *name* (case-insensitive)."""
    configs = load_run_configurations(project_root, config)
    # Case-insensitive lookup makes ``raccoon run Dev`` work too.
    lower = name.lower()
    for cfg_name, cfg in configs.items():
        if cfg_name.lower() == lower:
            return cfg
    available = ", ".join(sorted(configs.keys()))
    raise ProjectError(
        f"Run configuration '{name}' not found. Available: {available}"
    )


def save_run_configurations(
    project_root: Path,
    configs: Dict[str, RunConfiguration],
    hidden_builtins: Optional[set[str]] = None,
) -> None:
    """Persist user-defined configurations to ``raccoon.project.yml``.

    Builtins are stripped — they live in code, not in the YAML. The
    ``hidden_builtins`` tombstone list lets the user hide a shipped
    preset (so neither the CLI nor the IDE offers it any more).
    """
    persistable = {
        name: cfg.to_dict()
        for name, cfg in configs.items()
        if not cfg.builtin
    }
    updates: Dict[str, Any] = {CONFIG_KEY: persistable}
    if hidden_builtins is not None:
        # Sorted for deterministic diffs in YAML.
        updates[HIDDEN_KEY] = sorted(hidden_builtins)
    save_project_keys(project_root, updates)


def _load_hidden_builtins(project_root: Path) -> set[str]:
    config = load_project_config(project_root)
    raw = config.get(HIDDEN_KEY) or []
    if not isinstance(raw, list):
        return set()
    return {str(n) for n in raw}


def upsert_run_configuration(
    project_root: Path,
    cfg: RunConfiguration,
) -> Dict[str, RunConfiguration]:
    """Insert or replace *cfg*, persist, and return the updated set.

    Upserting an entry also un-hides a tombstoned builtin of the same
    name — explicit user action wins over the hide-list.
    """
    existing = load_run_configurations(project_root)
    cfg.builtin = False
    existing[cfg.name] = cfg
    hidden = _load_hidden_builtins(project_root)
    hidden.discard(cfg.name)
    save_run_configurations(project_root, existing, hidden_builtins=hidden)
    return existing


def delete_run_configuration(
    project_root: Path,
    name: str,
) -> Dict[str, RunConfiguration]:
    """Remove a configuration from the project.

    User-defined entries are stripped from ``run_configurations:``.
    Builtins are tombstoned in ``hidden_run_configurations:`` so they
    disappear from both the CLI and the IDE until the user un-hides them
    (which currently only happens by re-creating an entry with the same
    name through the IDE).
    """
    builtins = _builtin_presets()
    user_names = _user_defined_names(project_root)
    if name not in builtins and name not in user_names:
        raise ProjectError(f"Run configuration '{name}' not found")

    existing = load_run_configurations(project_root)
    existing.pop(name, None)
    hidden = _load_hidden_builtins(project_root)
    if name in builtins:
        hidden.add(name)
    save_run_configurations(project_root, existing, hidden_builtins=hidden)
    return load_run_configurations(project_root)


def _user_defined_names(project_root: Path) -> set[str]:
    config = load_project_config(project_root)
    raw = config.get(CONFIG_KEY) or {}
    if not isinstance(raw, dict):
        return set()
    return {str(k) for k in raw.keys()}
