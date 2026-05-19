from __future__ import annotations

from pathlib import Path

from agentlab.artifacts import ArtifactStore
from agentlab.config import AppConfig
from agentlab.provenance import ProvenanceBuilder


def test_provenance_builder_emits_slsa_inspired_statement(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = tmp_path / "runs" / "run-1"
    store = ArtifactStore(run_dir, "run-1")
    store.write_json("example", {"ok": True})
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=repo,
        workspace_root=tmp_path / "runs",
    )

    statement = ProvenanceBuilder(config, run_id="run-1", run_dir=run_dir, artifacts=store).build()

    assert statement.statement_type == "https://in-toto.io/Statement/v1"
    assert statement.predicate_type == "https://slsa.dev/provenance/v1"
    assert statement.predicate["buildDefinition"]["buildType"] == "https://agentlab.local/provenance/run/v1"
    assert statement.predicate["runDetails"]["metadata"]["invocationId"] == "run-1"
    assert "example.json" in statement.predicate["agentlab"]["artifact_hashes"]
