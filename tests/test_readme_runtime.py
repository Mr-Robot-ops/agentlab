from __future__ import annotations

from pathlib import Path


def readme() -> str:
    return Path("README.md").read_text(encoding="utf-8")


def test_readme_contains_runtime_choice_and_quickstarts() -> None:
    text = readme()

    assert "## Runtime waehlen" in text
    assert "| Kubernetes | empfohlener Betrieb | kubectl, Image, Cluster | scripts/bootstrap_k8s.py |" in text
    assert "## Quickstart A: Kubernetes Runtime" in text
    assert "## Quickstart B: Docker Compose Runtime" in text
    assert "## Quickstart C: Local Python Runtime" in text


def test_readme_documents_shared_commands_and_token_handling() -> None:
    text = readme()

    for command in ("agentlab doctor", "agentlab dry-run", "agentlab index", "agentlab steward", "agentlab plan", "agentlab full-flow"):
        assert command in text
    assert "GitLab Tokens werden niemals in `config.yaml`" in text
    assert "Kubernetes: Secret `agentlab-secrets`" in text
    assert "Docker Compose: `.env.agentlab`" in text


def test_readme_keeps_kubernetes_primary_and_local_python_development_scoped() -> None:
    text = readme()

    assert "Kubernetes ist der empfohlene Betrieb" in text
    assert "Local Python ist fuer Entwicklung, Debugging, Codex und lokale Tests gedacht" in text
    assert "kein `/var/run/docker.sock` Mount" in text
    assert "Komodo ist optional" in text


def test_scheduler_docs_cover_operational_troubleshooting() -> None:
    text = Path("docs/scheduler.md").read_text(encoding="utf-8")

    assert 'project_id: "5"' in text
    assert "path_not_allowed" in text
    assert "disallowed_paths" in text
    assert "default_branch_unchanged" in text
    assert "scheduler-reset-state" in text
