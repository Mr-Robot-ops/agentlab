from __future__ import annotations

from pathlib import Path

from agentlab.tools.docker_safety import DockerSafetyScanner


def test_runtime_dockerfile_installs_rust_toolchain() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    install_block = content.split("apt-get install -y --no-install-recommends", 1)[1].split("&& rm -rf /var/lib/apt/lists/*", 1)[0]

    assert "FROM rust:1-slim-bookworm AS rust-toolchain" in content
    assert "COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup" in content
    assert "COPY --from=rust-toolchain /usr/local/cargo/bin /usr/local/cargo/bin" in content
    assert "build-essential" in install_block
    assert "cargo" not in install_block
    assert "rustc" not in install_block
    assert "cargo --version" in content
    assert "rustc --version" in content
    assert "rm -rf /var/lib/apt/lists/*" in content


def test_runtime_rust_toolchain_smoke_checks_are_documented() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = "\n".join(
        [
            (root / "README.md").read_text(encoding="utf-8"),
            (root / "docs" / "release-upgrade.md").read_text(encoding="utf-8"),
        ]
    )

    assert "docker run --rm <image> cargo --version" in docs
    assert "docker run --rm <image> rustc --version" in docs
    assert "Cargo.lock version 4" in docs


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


def test_docker_safety_scanner_handles_compose_scalar_and_mapping_forms(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        """
services:
  app:
    image: alpine
    privileged: "true"
    pid: HOST
    security_opt: seccomp=unconfined
    extra_hosts:
      host.docker.internal: host-gateway
    env_file:
      path: secrets.env
""",
        encoding="utf-8",
    )

    findings = DockerSafetyScanner(tmp_path).scan_compose_file()
    by_title = {finding.title: finding for finding in findings}

    assert by_title["Privileged compose service"].blocked is True
    assert by_title["Host namespace requested: pid"].blocked is True
    assert by_title["Unconfined security profile"].blocked is True
    assert by_title["Host gateway exposed to container"].severity == "medium"
    assert by_title["Secret-like env_file referenced"].severity == "high"
