from pathlib import Path

import pytest

from agentlab.config import AppConfig
from agentlab.tools.common import ToolError
from agentlab.workspace import WorkspaceManager


def config(tmp_path: Path, **overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": tmp_path / "repo",
        "workspace_root": tmp_path / "runs",
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def test_existing_checkout_is_reused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    result = WorkspaceManager(config(tmp_path)).prepare()
    assert result["status"] == "existing"
    assert result["repo_path"] == str(repo.resolve())


def test_missing_checkout_requires_clone_flag(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="clone_target_repo is false"):
        WorkspaceManager(config(tmp_path)).prepare()


def test_embedded_https_credentials_are_rejected(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        clone_target_repo=True,
        target_repo_url="https://oauth2:secret@gitlab.local/group/project.git",
    )
    with pytest.raises(ToolError, match="embedded git credentials"):
        WorkspaceManager(cfg).prepare()
