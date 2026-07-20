"""Inspect active Traefik routes attached to the shared Docker network."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

import click

HOST_RULE = re.compile(r"Host\(\s*`([^`]+)`\s*\)")


@dataclass(frozen=True)
class Route:
    hostname: str
    location: str


def proxy_is_running() -> bool:
    """Return whether the fixed shared proxy container is already running."""
    result = _docker(
        [
            "ps",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=localghost",
            "--filter",
            "label=com.docker.compose.service=traefik",
        ]
    )
    return bool(result.stdout.strip())


def active_routes() -> list[Route]:
    """Return directly declared host routes for running opted-in containers."""
    listed = _docker(
        ["ps", "--quiet", "--filter", "label=traefik.enable=true"]
    )
    identifiers = listed.stdout.split()
    if not identifiers:
        return []
    inspected = _docker(["inspect", *identifiers])
    try:
        containers = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            "Docker returned invalid container inspection data"
        ) from exc

    routes: dict[str, Route] = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict):
            continue
        location = _location(labels, container)
        for key, rule in labels.items():
            if not key.startswith("traefik.http.routers.") or not key.endswith(".rule"):
                continue
            for hostname in HOST_RULE.findall(str(rule)):
                routes.setdefault(hostname, Route(hostname, location))
    return sorted(routes.values(), key=lambda route: route.hostname)


def _location(labels: dict[str, object], container: dict[str, object]) -> str:
    source_path = labels.get("io.localghost.source-path")
    if isinstance(source_path, str) and source_path:
        return source_path
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    if isinstance(project, str) and isinstance(service, str):
        return f"{project} / {service}"
    name = container.get("Name")
    return str(name).lstrip("/") if name else "Docker container"


def _docker(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *arguments], check=False, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise click.ClickException("docker is required") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise click.ClickException(detail or "Docker route inspection failed")
    return result
