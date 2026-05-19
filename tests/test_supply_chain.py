from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig
from agentlab.repo_indexer import RepoIndexer
from agentlab.supply_chain import SupplyChainAnalyzer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_supply_chain_analyzer_builds_sbom_and_flags_missing_lockfiles(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(repo / "pyproject.toml", '[project]\ndependencies = ["httpx==0.27.0", "pydantic>=2.8"]\n')
    _write(repo / "package.json", '{"dependencies": {"vite": "^5.0.0"}}')
    _write(repo / "secret.example.yaml", "kind: Secret\n")

    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=repo,
        workspace_root=tmp_path / "runs",
        require_lockfiles_for_merge=True,
    )
    index = RepoIndexer(config).build_index()
    report = SupplyChainAnalyzer(config, index).analyze()

    assert report.passed is False
    assert report.sbom.bomFormat == "CycloneDX"
    assert report.components_count == 3
    assert "pyproject.toml" in report.missing_lockfiles
    assert "package.json" in report.missing_lockfiles
    assert any(component.name == "httpx" and component.version == "0.27.0" for component in report.sbom.components)
    assert any(finding.blocked for finding in report.findings if finding.path == "pyproject.toml")
    assert any(finding.title == "Secret-like file detected in repository index" for finding in report.findings)
