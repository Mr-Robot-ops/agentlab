from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentlab.models import Finding, FindingSeverity


BLOCKED_VOLUME_TARGETS = ("/", "/root", "/home", "/var/run/docker.sock", "/etc", "/var/lib")
SECRET_ENV_HINTS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")


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
        if service.get("privileged") is True or str(service.get("privileged", "")).lower() == "true":
            findings.append(self._blocked(path, "Privileged compose service", "privileged: true is not allowed."))
        for key in ("network_mode", "pid", "ipc"):
            if str(service.get(key, "")).lower() == "host":
                findings.append(self._blocked(path, f"Host namespace requested: {key}", f"{key}: host is not allowed."))
        if service.get("cap_add"):
            findings.append(self._blocked(path, "Linux capabilities added", "cap_add requires explicit policy support and is blocked."))
        if service.get("devices"):
            findings.append(self._blocked(path, "Host devices mounted", "devices entries require explicit policy support and are blocked."))
        user = str(service.get("user", "")).strip().lower()
        if user in {"0", "root"}:
            findings.append(
                self._finding(
                    path,
                    "Container runs as root",
                    "user is root/0. Prefer an explicit non-root UID for agent-managed workloads.",
                    FindingSeverity.HIGH,
                    blocked=False,
                )
            )
        for option in self._list_values(service.get("security_opt")):
            normalized = str(option).lower()
            if normalized in {"apparmor=unconfined", "seccomp=unconfined"}:
                findings.append(self._blocked(path, "Unconfined security profile", f"security_opt {option} is not allowed."))
        for host in self._list_values(service.get("extra_hosts")):
            if "host-gateway" in str(host).lower():
                findings.append(
                    self._finding(
                        path,
                        "Host gateway exposed to container",
                        f"extra_hosts entry uses host-gateway: {host}",
                        FindingSeverity.MEDIUM,
                        blocked=False,
                    )
                )
        findings.extend(self._scan_environment(path, service.get("environment")))
        findings.extend(self._scan_env_files(path, service.get("env_file")))
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

    def _scan_environment(self, path: str, environment: Any) -> list[Finding]:
        keys: list[str] = []
        if isinstance(environment, dict):
            keys = [str(key) for key in environment]
        elif isinstance(environment, list):
            for item in environment:
                key = str(item).split("=", 1)[0]
                if key:
                    keys.append(key)
        return [
            self._finding(
                path,
                "Secret-like environment variable name",
                f"Environment key looks secret-like: {key}",
                FindingSeverity.HIGH,
                blocked=False,
            )
            for key in keys
            if any(hint in key.upper() for hint in SECRET_ENV_HINTS)
        ]

    def _scan_env_files(self, path: str, env_file: Any) -> list[Finding]:
        if env_file is None:
            return []
        values = self._list_values(env_file)
        findings = []
        for value in values:
            text = str(value).lower()
            if text.endswith(".env") or "secret" in text or "token" in text or "password" in text:
                findings.append(
                    self._finding(
                        path,
                        "Secret-like env_file referenced",
                        f"env_file points to a secret-like file: {value}",
                        FindingSeverity.HIGH,
                        blocked=False,
                    )
                )
        return findings

    @staticmethod
    def _list_values(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
        return [value]

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
        return DockerSafetyScanner._finding(path, title, description, FindingSeverity.CRITICAL, blocked=True)

    @staticmethod
    def _finding(path: str, title: str, description: str, severity: FindingSeverity, *, blocked: bool) -> Finding:
        return Finding(
            tool="docker-safety",
            severity=severity,
            title=title,
            path=path,
            description=description,
            blocked=blocked,
        )
