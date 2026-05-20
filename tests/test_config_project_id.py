from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig, derive_project_path_from_repo_url, gitlab_project_api_id, normalize_project_id


def test_numeric_project_id_remains_usable() -> None:
    config = AppConfig(gitlab_url="https://gitlab.example.com", project_id=123, target_repo_path=Path("."))

    assert config.project_id == 123
    assert gitlab_project_api_id(config.project_id) == 123


def test_group_project_path_is_accepted() -> None:
    config = AppConfig(gitlab_url="https://gitlab.example.com", project_id="group/project", target_repo_path=Path("."))

    assert config.project_id == "group/project"
    assert gitlab_project_api_id(config.project_id) == "group%2Fproject"


def test_url_encoded_project_path_is_normalized() -> None:
    assert normalize_project_id("group%2Fproject") == "group/project"


def test_project_id_can_be_derived_from_target_repo_url() -> None:
    config = AppConfig(
        gitlab_url="https://gitlab.example.com",
        target_repo_path=Path("."),
        target_repo_url="https://gitlab.example.com/group/project.git",
    )

    assert config.project_id == "group/project"


def test_repo_url_derivation_handles_scp_style_urls() -> None:
    assert derive_project_path_from_repo_url("git@gitlab.example.com:group/sub/project.git") == "group/sub/project"
