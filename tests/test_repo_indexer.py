from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.repo_indexer import RepoIndexer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_repo_indexer_detects_whole_repo_signals(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(repo / "pyproject.toml", '[project]\ndependencies = ["typer", "pytest", "pydantic"]\n')
    _write(repo / "README.md", "# Demo\n")
    _write(repo / "agentlab" / "main.py", "# TODO: tighten command handling\nprint('ok')\n")
    _write(repo / "tests" / "test_main.py", "def test_ok():\n    assert True\n")
    _write(repo / "Dockerfile", "FROM python:3.12-slim\n")
    _write(repo / "deploy" / "kubernetes" / "app.yaml", "apiVersion: v1\nkind: Pod\n")
    _write(repo / "deploy" / "kubernetes" / "secret.example.yaml", "kind: Secret\n")

    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=repo,
        workspace_root=tmp_path / "runs",
    )

    indexer = RepoIndexer(config)
    index = indexer.build_index()
    architecture = indexer.summarize_architecture(index)

    assert index.total_files == 8
    assert index.skipped_files == 1
    assert "python" in index.languages
    assert "pyproject.toml" in index.manifests
    assert "tests/test_main.py" in index.test_files
    assert "Dockerfile" in index.docker_files
    assert "deploy/kubernetes/app.yaml" in index.kubernetes_files
    assert "deploy/kubernetes/secret.example.yaml" in index.security_files
    assert index.todos[0].path == "agentlab/main.py"
    assert architecture.project_type == "python cli"
    assert "typer" in architecture.frameworks
    assert architecture.test_strategy == "python tests detected, likely pytest-compatible"
    assert "secret-like files detected; changes require human review" in architecture.risks
