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
