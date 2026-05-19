from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentlab.artifacts import ArtifactStore
from agentlab.audit import redact_secrets
from agentlab.config import AppConfig
from agentlab.models import ProvenanceStatement, ProvenanceSubject
from agentlab.tools.common import run_subprocess


class ProvenanceBuilder:
    def __init__(self, config: AppConfig, *, run_id: str, run_dir: Path, artifacts: ArtifactStore) -> None:
        self.config = config
        self.run_id = run_id
        self.run_dir = run_dir
        self.artifacts = artifacts
        self.repo_path = config.target_repo_path.resolve()

    def build(self) -> ProvenanceStatement:
        commit_sha = self._git(["rev-parse", "HEAD"])
        status = self._git(["status", "--porcelain"])
        remote_url = self._git(["config", "--get", "remote.origin.url"]) or self.config.target_repo_url
        subject = []
        if commit_sha:
            subject.append(ProvenanceSubject(name=self.repo_path.name, digest={"gitCommit": commit_sha}))

        manifest = self.artifacts.read_manifest()
        artifact_hashes = {artifact.name: artifact.sha256 for artifact in manifest.artifacts}
        config_payload = redact_secrets(self.config.model_dump(mode="json"))
        config_hash = _sha256_json(config_payload)
        predicate: dict[str, Any] = {
            "buildDefinition": {
                "buildType": "https://agentlab.local/provenance/run/v1",
                "externalParameters": {
                    "mode": "agentlab-run",
                    "default_branch": self.config.default_branch,
                    "dry_run_safe_defaults": {
                        "auto_merge_enabled": self.config.auto_merge_enabled,
                        "direct_main_push_enabled": self.config.direct_main_push_enabled,
                        "push_agent_branches_enabled": self.config.push_agent_branches_enabled,
                    },
                },
                "internalParameters": {
                    "config_sha256": config_hash,
                    "workspace_root": str(self.config.workspace_root),
                },
            },
            "runDetails": {
                "builder": {"id": "agentlab/local-or-kubernetes"},
                "metadata": {
                    "invocationId": self.run_id,
                    "startedOn": datetime.now(UTC).isoformat(),
                    "runDir": str(self.run_dir),
                    "sourceDirty": bool(status.strip()) if status is not None else None,
                },
            },
            "materials": [
                {
                    "uri": remote_url or str(self.repo_path),
                    "digest": {"gitCommit": commit_sha} if commit_sha else {},
                }
            ],
            "agentlab": {
                "artifact_hashes": artifact_hashes,
                "audit_file": str(self.run_dir / self.config.audit_file),
                "policy": {
                    "max_changed_files": self.config.max_changed_files,
                    "max_added_lines": self.config.max_added_lines,
                    "max_deleted_lines": self.config.max_deleted_lines,
                    "max_risk_score_for_merge": self.config.max_risk_score_for_merge,
                    "require_lockfiles_for_merge": self.config.require_lockfiles_for_merge,
                },
            },
        }
        return ProvenanceStatement(subject=subject, predicate=predicate)

    def _git(self, args: list[str]) -> str | None:
        if not (self.repo_path / ".git").exists():
            return None
        result = run_subprocess(["git", *args], cwd=self.repo_path, timeout_seconds=60)
        if not result.ok:
            return None
        return result.stdout.strip()


def _sha256_json(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    data = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
