"""Docker Compose model inspection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import click


def resolve_compose(files: tuple[Path, ...]) -> dict[str, Any]:
    """Return Docker Compose's normalized JSON model."""
    command = ["docker", "compose"]
    for compose_file in files:
        command.extend(["--file", str(compose_file)])
    command.extend(["config", "--format", "json"])

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise click.ClickException("docker is required and was not found") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise click.ClickException(
            f"Docker Compose could not resolve the project:\n{detail}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException("Docker Compose returned invalid JSON") from exc


def declared_ports(service: dict[str, Any]) -> list[int]:
    """Find container ports declared by Compose or existing Traefik labels."""
    ports: set[int] = set()

    for item in service.get("ports", []):
        target = item.get("target") if isinstance(item, dict) else item
        _add_port(ports, target)

    for item in service.get("expose", []):
        _add_port(ports, item)

    labels = service.get("labels") or {}
    if isinstance(labels, dict):
        suffix = ".loadbalancer.server.port"
        for key, value in labels.items():
            if key.startswith("traefik.http.services.") and key.endswith(suffix):
                _add_port(ports, value)

    return sorted(ports)


def _add_port(ports: set[int], value: Any) -> None:
    try:
        port = int(str(value).split("/")[0])
    except (TypeError, ValueError):
        return
    if 1 <= port <= 65535:
        ports.add(port)
