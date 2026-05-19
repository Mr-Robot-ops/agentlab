from __future__ import annotations

from pathlib import Path

from agentlab.tools.docker_safety import DockerSafetyScanner


def test_docker_safety_scanner_blocks_unsafe_compose_settings(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  app:
    image: alpine
    privileged: true
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
""",
        encoding="utf-8",
    )

    findings = DockerSafetyScanner(tmp_path).scan_compose_file()
    titles = {finding.title for finding in findings}

    assert "Privileged compose service" in titles
    assert "Host namespace requested: network_mode" in titles
    assert "Unsafe compose volume mount" in titles
    assert all(finding.blocked for finding in findings)


def test_docker_safety_scanner_allows_regular_named_volume(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  app:
    image: alpine
    volumes:
      - app-cache:/app/cache
volumes:
  app-cache: {}
""",
        encoding="utf-8",
    )

    assert DockerSafetyScanner(tmp_path).scan_compose_file() == []


def test_docker_safety_scanner_reports_runtime_and_secret_hints(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  app:
    image: alpine
    user: "0"
    security_opt:
      - seccomp=unconfined
    extra_hosts:
      - host.docker.internal:host-gateway
    environment:
      API_KEY: example
      NORMAL: value
    env_file:
      - .env
""",
        encoding="utf-8",
    )

    findings = DockerSafetyScanner(tmp_path).scan_compose_file()
    by_title = {finding.title: finding for finding in findings}

    assert by_title["Container runs as root"].blocked is False
    assert by_title["Container runs as root"].severity == "high"
    assert by_title["Unconfined security profile"].blocked is True
    assert by_title["Host gateway exposed to container"].severity == "medium"
    assert by_title["Secret-like environment variable name"].severity == "high"
    assert by_title["Secret-like env_file referenced"].severity == "high"
