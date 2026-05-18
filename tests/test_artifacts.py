from agentlab.artifacts import ArtifactStore


def test_artifact_store_writes_manifest_and_redacts_secrets(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "run-1", "run-1")

    record = store.write_json("result", {"token": "abc", "message": "ok"})
    manifest = store.read_manifest()

    assert record.name == "result.json"
    assert manifest.artifacts[0].name == "result.json"
    assert "abc" not in (tmp_path / "run-1" / "artifacts" / "result.json").read_text(encoding="utf-8")
