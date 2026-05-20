from agentlab.artifacts import ArtifactStore


def test_artifact_store_writes_manifest_and_redacts_secrets(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "run-1", "run-1")

    record = store.write_json("result", {"token": "abc", "message": "ok"})
    text_record = store.write_text("patch.diff", "+password=secret\n+ok\n")
    manifest = store.read_manifest()

    assert record.name == "result.json"
    assert text_record.name == "patch.diff"
    assert [item.name for item in manifest.artifacts] == ["result.json", "patch.diff"]
    assert "abc" not in (tmp_path / "run-1" / "artifacts" / "result.json").read_text(encoding="utf-8")
    assert "secret" not in (tmp_path / "run-1" / "artifacts" / "patch.diff").read_text(encoding="utf-8")
