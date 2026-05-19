from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentlab.models import Finding, FindingSeverity


BLOCKED_VOLUME_TARGETS = ("/", "/root", "/home", "/var/run/docker.sock", "/etc", "/var/lib")


class DockerSafetyScanner:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).resolve()

    def scan_compose_file(self, compose_file: str = "docker-compose.yml") -> list[Finding]:
        path = self.repo_path / compose_file
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            return [
                Finding(
                    tool="docker-safety",
                    severity=FindingSeverity.HIGH,
                    title="Compose file could not be parsed",
                    path=compose_file,
                    description=str(exc),
                    blocked=True,
                )
            ]
        services = data.get("services", {})
        if not isinstance(services, dict):
            return []
        findings: list[Finding] = []
        for service_name, raw_service in services.items():
            if not isinstance(raw_service, dict):
                continue
            service_path = f"{compose_file}:services.{service_name}"
            findings.extend(self._scan_service(service_path, raw_service))
        return findings

    def _scan_service(self, path: str, service: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        if service.get("privileged") is True:
            findings.append(self._blocked(path, "Privileged compose service", "privileged: true is not allowed."))
        for key in ("network_mode", "pid", "ipc"):
            if service.get(key) == "host":
                findings.append(self._blocked(path, f"Host namespace requested: {key}", f"{key}: host is not allowed."))
        if service.get("cap_add"):
            findings.append(self._blocked(path, "Linux capabilities added", "cap_add requires explicit policy support and is blocked."))
        if service.get("devices"):
            findings.append(self._blocked(path, "Host devices mounted", "devices entries require explicit policy support and are blocked."))
        for volume in service.get("volumes", []) or []:
            target = self._volume_target(volume)
            source = self._volume_source(volume)
            if self._blocked_volume_path(target) or self._blocked_volume_path(source):
                findings.append(
                    self._blocked(
                        path,
                        "Unsafe compose volume mount",
                        f"Volume mount touches blocked host/container path: {volume}",
                    )
                )
        return findings

    @staticmethod
    def _volume_target(volume: Any) -> str:
        if isinstance(volume, str):
            parts = volume.split(":")
            return parts[1] if len(parts) > 1 else parts[0]
        if isinstance(volume, dict):
            target = volume.get("target") or volume.get("dst") or volume.get("destination")
            return str(target or "")
        return ""

    @staticmethod
    def _volume_source(volume: Any) -> str:
        if isinstance(volume, str):
            parts = volume.split(":")
            return parts[0] if len(parts) > 1 else ""
        if isinstance(volume, dict):
            source = volume.get("source") or volume.get("src")
            return str(source or "")
        return ""

    @staticmethod
    def _blocked_volume_path(path: str) -> bool:
        normalized = path.replace("\\", "/").rstrip("/") or "/"
        for item in BLOCKED_VOLUME_TARGETS:
            if item == "/" and normalized == "/":
                return True
            if item != "/" and (normalized == item or normalized.startswith(item.rstrip("/") + "/")):
                return True
        return False

    @staticmethod
    def _blocked(path: str, title: str, description: str) -> Finding:
        return Finding(
            tool="docker-safety",
            severity=FindingSeverity.CRITICAL,
            title=title,
            path=path,
            description=description,
            blocked=True,
        )
