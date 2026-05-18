from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentlab.audit import redact_secrets
from agentlab.models import ArtifactManifest, ArtifactRecord


class ArtifactStore:
    def __init__(self, run_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.artifacts_dir = self.run_dir / "artifacts"
        self.manifest_path = self.artifacts_dir / "manifest.json"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, payload: Any) -> ArtifactRecord:
        safe_name = self._safe_name(name)
        path = self.artifacts_dir / safe_name
        serializable = self._payload(payload)
        path.write_text(serializable, encoding="utf-8")
        record = ArtifactRecord(
            name=safe_name,
            path=str(path),
            sha256=hashlib.sha256(serializable.encode("utf-8")).hexdigest(),
        )
        manifest = self.read_manifest()
        kept = [item for item in manifest.artifacts if item.name != safe_name]
        manifest = manifest.model_copy(update={"artifacts": [*kept, record]})
        self.manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        return record

    def read_manifest(self) -> ArtifactManifest:
        if not self.manifest_path.exists():
            return ArtifactManifest(run_id=self.run_id)
        return ArtifactManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    def _payload(self, payload: Any) -> str:
        if isinstance(payload, BaseModel):
            data = payload.model_dump(mode="json")
        else:
            data = payload
        return _json(data)

    @staticmethod
    def _safe_name(name: str) -> str:
        if not name.endswith(".json"):
            name = f"{name}.json"
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe artifact name: {name}")
        return name


def _json(payload: Any) -> str:
    import json

    return json.dumps(redact_secrets(payload), indent=2, ensure_ascii=True, default=str)
