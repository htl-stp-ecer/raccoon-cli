"""Project-owned systemd service deployment.

Projects can declare long-lived helper processes in ``raccoon.project.yml``:

services:
  vision:
    module: src.daemons.vision
    restart: always
    after_sync: restart_if_changed
    required_for_run: true

The raccoon server deploys these units after project sync and before running
``src.main``. The implementation is intentionally generic; camera/vision logic
stays in the project repository.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raccoon_cli.fingerprint import compute_fingerprint, default_exclude_patterns


SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
DEFAULT_RESTART = "always"
DEFAULT_AFTER_SYNC = "restart_if_changed"


@dataclass(frozen=True)
class ProjectService:
    """Normalized project service definition."""

    name: str
    project_id: str
    project_path: Path
    module: str | None = None
    command: str | list[str] | None = None
    user: str = "pi"
    group: str | None = None
    restart: str = DEFAULT_RESTART
    restart_sec: int = 1
    after_sync: str = DEFAULT_AFTER_SYNC
    required_for_run: bool = True
    env: dict[str, str] = field(default_factory=dict)
    watch: list[str] = field(default_factory=list)

    @property
    def systemd_name(self) -> str:
        safe_project_id = re.sub(r"[^a-zA-Z0-9_-]", "-", self.project_id)
        return f"raccoon-project-{safe_project_id}-{self.name}.service"


def load_project_services(config: dict[str, Any], project_path: Path) -> list[ProjectService]:
    """Parse ``services`` from project config.

    Returns an empty list when no services are configured.
    """
    raw_services = config.get("services") or {}
    if not isinstance(raw_services, dict):
        raise ValueError("raccoon.project.yml key 'services' must be a mapping")

    project_id = str(config.get("uuid") or project_path.name)
    services: list[ProjectService] = []

    for name, raw in raw_services.items():
        if not isinstance(name, str) or not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Invalid service name {name!r}; use letters, numbers, '_' or '-'"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"Service {name!r} must be a mapping")

        module = raw.get("module")
        command = raw.get("command")
        if bool(module) == bool(command):
            raise ValueError(
                f"Service {name!r} must define exactly one of 'module' or 'command'"
            )
        if module is not None and not isinstance(module, str):
            raise ValueError(f"Service {name!r} field 'module' must be a string")
        if command is not None and not isinstance(command, (str, list)):
            raise ValueError(f"Service {name!r} field 'command' must be a string or list")

        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"Service {name!r} field 'env' must be a mapping")

        watch = raw.get("watch") or []
        if isinstance(watch, str):
            watch = [watch]
        if not isinstance(watch, list) or not all(isinstance(p, str) for p in watch):
            raise ValueError(f"Service {name!r} field 'watch' must be a string list")

        services.append(
            ProjectService(
                name=name,
                project_id=project_id,
                project_path=project_path,
                module=module,
                command=command,
                user=str(raw.get("user") or "pi"),
                group=str(raw["group"]) if raw.get("group") else None,
                restart=str(raw.get("restart") or DEFAULT_RESTART),
                restart_sec=int(raw.get("restart_sec") or 1),
                after_sync=str(raw.get("after_sync") or DEFAULT_AFTER_SYNC),
                required_for_run=bool(raw.get("required_for_run", True)),
                env={str(k): str(v) for k, v in env.items()},
                watch=watch,
            )
        )

    return services


def render_systemd_unit(service: ProjectService, python_executable: str = "/usr/bin/python3") -> str:
    """Render a systemd unit for a project service."""
    if service.module:
        exec_start = " ".join(
            shlex.quote(part)
            for part in [python_executable, "-m", service.module]
        )
    elif isinstance(service.command, list):
        exec_start = " ".join(shlex.quote(str(part)) for part in service.command)
    else:
        exec_start = str(service.command)

    lines = [
        "[Unit]",
        f"Description=Raccoon project service {service.name} ({service.project_id})",
        "After=network.target raccoon.service",
        "",
        "[Service]",
        "Type=simple",
        f"User={service.user}",
    ]
    if service.group:
        lines.append(f"Group={service.group}")

    lines.extend(
        [
            f"WorkingDirectory={service.project_path}",
            'Environment="PYTHONUNBUFFERED=1"',
        ]
    )
    for key, value in sorted(service.env.items()):
        escaped = str(value).replace('"', '\\"')
        lines.append(f'Environment="{key}={escaped}"')

    lines.extend(
        [
            f"ExecStart={exec_start}",
            f"Restart={service.restart}",
            f"RestartSec={service.restart_sec}",
            "StandardOutput=journal",
            "StandardError=journal",
            f"SyslogIdentifier={service.systemd_name.removesuffix('.service')}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    return "\n".join(lines)


def service_content_hash(service: ProjectService, unit_text: str) -> str:
    """Hash service definition plus watched project files.

    If ``watch`` is omitted, the whole project fingerprint is included. That is
    conservative but predictable: any project update can restart the daemon.
    """
    payload = {
        "unit": unit_text,
        "service": {
            "name": service.name,
            "module": service.module,
            "command": service.command,
            "env": service.env,
            "restart": service.restart,
            "restart_sec": service.restart_sec,
            "after_sync": service.after_sync,
            "required_for_run": service.required_for_run,
            "watch": service.watch,
        },
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))

    if service.watch:
        for rel in sorted(service.watch):
            path = (service.project_path / rel).resolve()
            try:
                if path.is_file():
                    h.update(rel.encode("utf-8"))
                    h.update(path.read_bytes())
            except OSError:
                continue
    else:
        fp = compute_fingerprint(service.project_path, default_exclude_patterns())
        h.update(fp.root_hash.encode("utf-8"))

    return h.hexdigest()


@dataclass(frozen=True)
class ServiceDeployResult:
    """Result of deploying a single project service.

    ``action`` is the systemctl verb actually issued (``restart`` for a real
    restart, ``start`` for a no-op when already running). ``first_deploy``
    means there was no previous digest on disk. ``digest_changed`` means the
    rendered unit + watched files differ from the last deploy.
    """

    name: str
    systemd_name: str
    action: str  # "restart" | "start"
    first_deploy: bool
    digest_changed: bool
    reason: str


def deploy_project_services(
    config: dict[str, Any],
    project_path: Path,
    *,
    runner=subprocess.run,
) -> list[ServiceDeployResult]:
    """Install/update configured project services and return per-service results."""
    services = load_project_services(config, project_path)
    results: list[ServiceDeployResult] = []
    if not services:
        return results

    state_dir = project_path / ".raccoon" / "services"
    state_dir.mkdir(parents=True, exist_ok=True)

    for service in services:
        unit_text = render_systemd_unit(service)
        digest = service_content_hash(service, unit_text)
        digest_path = state_dir / f"{service.name}.sha256"
        previous_digest = digest_path.read_text().strip() if digest_path.exists() else ""
        first_deploy = previous_digest == ""
        digest_changed = previous_digest != digest
        unit_path = f"/etc/systemd/system/{service.systemd_name}"
        if service.after_sync not in {"restart", "restart_if_changed", "leave_running"}:
            raise ValueError(
                f"Service {service.name!r} has invalid after_sync={service.after_sync!r}"
            )
        should_restart = (
            service.after_sync == "restart"
            or (service.after_sync == "restart_if_changed" and digest_changed)
        )

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(unit_text)
            tmp_path = tmp.name

        try:
            runner(["sudo", "install", "-m", "0644", tmp_path, unit_path], check=True)
            runner(["sudo", "systemctl", "daemon-reload"], check=True)
            runner(["sudo", "systemctl", "enable", service.systemd_name], check=True)

            if service.after_sync == "leave_running":
                runner(["sudo", "systemctl", "start", service.systemd_name], check=True)
                action = "start"
                reason = "after_sync=leave_running"
            elif should_restart:
                runner(["sudo", "systemctl", "restart", service.systemd_name], check=True)
                action = "restart"
                if first_deploy:
                    reason = "first deploy"
                elif service.after_sync == "restart":
                    reason = "after_sync=restart"
                else:
                    reason = "watched files changed"
            else:
                runner(["sudo", "systemctl", "start", service.systemd_name], check=True)
                action = "start"
                reason = "unchanged (after_sync=restart_if_changed)"

            if service.required_for_run:
                runner(["sudo", "systemctl", "is-active", "--quiet", service.systemd_name], check=True)
            digest_path.write_text(digest + "\n", encoding="utf-8")
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        results.append(
            ServiceDeployResult(
                name=service.name,
                systemd_name=service.systemd_name,
                action=action,
                first_deploy=first_deploy,
                digest_changed=digest_changed,
                reason=reason,
            )
        )

    return results
