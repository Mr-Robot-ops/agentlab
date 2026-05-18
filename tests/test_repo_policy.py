from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import RepoPolicy
from agentlab.repo_policy import apply_repo_policy, load_repo_policy


def config(tmp_path: Path, **overrides: object) -> AppConfig:
    base = {
        "gitlab_url": "https://gitlab.example.com",
        "project_id": 1,
        "target_repo_path": tmp_path / "repo",
        "workspace_root": tmp_path / "runs",
        "protected_paths": ["infra/prod"],
        "max_changed_files": 20,
        "max_risk_score_for_merge": 60,
        "direct_main_push_enabled": True,
        "auto_merge_enabled": True,
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


def test_repo_policy_only_tightens_config(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    policy = RepoPolicy(
        protected_paths=["secrets"],
        allowed_task_types=["bugfix", "docs"],
        forbidden_task_types=["infra"],
        max_changed_files=5,
        max_risk_score_for_merge=10,
        block_direct_main_push=True,
    )

    merged = apply_repo_policy(cfg, policy)

    assert merged.protected_paths == ["infra/prod", "secrets"]
    assert merged.allowed_task_types == ["bugfix", "docs"]
    assert merged.forbidden_task_types == ["infra"]
    assert merged.max_changed_files == 5
    assert merged.max_risk_score_for_merge == 10
    assert merged.direct_main_push_enabled is False
    assert merged.auto_merge_enabled is True


def test_repo_policy_loads_from_target_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".agentlab.yaml").write_text(
        "version: 1\nprotected_paths:\n  - secrets\nrequired_test_commands:\n  - python -m pytest\n",
        encoding="utf-8",
    )

    policy = load_repo_policy(repo, ".agentlab.yaml")

    assert policy is not None
    assert policy.protected_paths == ["secrets"]
    assert policy.required_test_commands == ["python -m pytest"]
