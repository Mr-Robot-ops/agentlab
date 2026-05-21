from __future__ import annotations

import json
from pathlib import Path

from agentlab.agents.docs_check import DocsCheckAgent
from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.models import ReportStatus
from agentlab.tools.file_tool import FileTool


def config(repo: Path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=repo,
        workspace_root=repo.parent / "runs",
        supply_chain_enabled=False,
        provenance_enabled=False,
    )


def write_readme(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text(content, encoding="utf-8")


def run_docs_check(repo: Path, *, artifacts: ArtifactStore | None = None):
    cfg = config(repo)
    return DocsCheckAgent(FileTool(repo, cfg), artifacts).run(["README.md"])


def test_broken_markdown_fence_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(repo, "# Demo\n\n```text\nmissing close\n")

    report = run_docs_check(repo)

    assert report.status == ReportStatus.FAILED
    assert report.checks["docs_check"] == "failed"
    assert any(finding.title == "Markdown fence is not closed" for finding in report.findings)


def test_malformed_markdown_heading_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(repo, "# Demo\n\n##Missing space\n")

    report = run_docs_check(repo)

    assert report.checks["docs_check"] == "failed"
    assert any(finding.title == "Malformed Markdown heading: missing space" for finding in report.findings)


def test_broken_tree_indentation_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(
        repo,
        "# Demo\n\n## Project Structure\n\n```text\n.\n+-- web/\n  +-- src/\n```\n",
    )

    report = run_docs_check(repo)

    assert report.checks["tree_blocks"] == "failed"
    assert any(finding.title == "Broken README tree indentation" for finding in report.findings)


def test_broken_tree_connector_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(
        repo,
        "# Demo\n\n## Project Structure\n\n```text\n.\n+- web/\n```\n",
    )

    report = run_docs_check(repo)

    assert report.checks["tree_blocks"] == "failed"
    assert any("connector" in finding.description for finding in report.findings)


def test_valid_readme_tree_passes_with_missing_structure_evidence_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(
        repo,
        "# Demo\n\n## Project Structure\n\n```text\n.\n+-- web/\n|   +-- src/\n|       +-- App.tsx\n```\n",
    )

    report = run_docs_check(repo)

    assert report.status == ReportStatus.PASSED
    assert report.checks["docs_check"] == "passed"
    assert report.checks["structure_evidence_check"] == "skipped"
    assert report.docs_check == "passed"
    assert report.structure_evidence_check == "skipped"
    assert report.check_statuses == {"docs_check": "passed", "structure_evidence_check": "skipped"}
    assert "generate project_structure_evidence.json" in report.recommendation


def test_failed_project_structure_evidence_with_removed_entries_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(
        repo,
        "# Demo\n\n## Project Structure\n\n```text\n.\n+-- web/\n|   +-- src/\n```\n",
    )
    store = ArtifactStore(tmp_path / "run", "run")
    (store.artifacts_dir / "project_structure_evidence.json").write_text(
        json.dumps(
            {
                "validation_status": "failed",
                "removed_existing_entries": ["rust-backend/src/routes/health.rs"],
                "collected_files": ["rust-backend/src/routes/health.rs"],
                "old_readme_block": "old",
                "proposed_readme_block": "new",
            }
        ),
        encoding="utf-8",
    )

    report = run_docs_check(repo, artifacts=store)

    assert report.status == ReportStatus.FAILED
    assert report.checks["tree_blocks"] == "passed"
    assert report.checks["docs_check"] == "failed"
    assert report.checks["structure_evidence_check"] == "failed"
    assert any(finding.title == "README project structure removes existing files" for finding in report.findings)


def test_removed_project_structure_entries_fail_even_when_validation_status_passed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(
        repo,
        "# Demo\n\n## Project Structure\n\n```text\n.\n+-- rust-backend/\n```\n",
    )
    store = ArtifactStore(tmp_path / "run", "run")
    (store.artifacts_dir / "project_structure_evidence.json").write_text(
        json.dumps({"validation_status": "passed", "removed_existing_entries": ["rust-backend/src/routes/health.rs"]}),
        encoding="utf-8",
    )

    report = run_docs_check(repo, artifacts=store)

    assert report.checks["structure_evidence_check"] == "failed"
    assert report.passed is False


def test_docs_check_report_contains_top_level_status_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_readme(repo, "# Demo\n")

    report = run_docs_check(repo).model_dump(mode="json")

    for key in ("status", "passed", "checks", "findings", "recommendation", "check_statuses", "docs_check", "structure_evidence_check"):
        assert key in report
