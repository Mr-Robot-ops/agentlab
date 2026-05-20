from __future__ import annotations

from pathlib import Path

from agentlab.config import AppConfig, AutoApproveConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.policies.auto_approval import AutoApprovalPolicy


def config(**auto_overrides: object) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=Path("."),
        workspace_root=Path(".runs"),
        auto_approve=AutoApproveConfig(enabled=True, **auto_overrides),
    )


def task(**overrides: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "docs-readme",
        "title": "Docs",
        "task_type": TaskType.DOCS,
        "risk_level": RiskLevel.LOW,
        "risk_score": 1,
        "affected_files": ["README.md"],
        "forbidden_actions": ["Do not change source code."],
        "test_requirements": [],
    }
    values.update(overrides)
    return AgentTask.model_validate(values)


def apply_one(item: AgentTask, cfg: AppConfig | None = None) -> tuple[AgentTask, dict[str, object]]:
    plan, report = AutoApprovalPolicy(cfg or config()).apply(TaskPlan(tasks=[item]))
    return plan.tasks[0], report


def test_auto_approve_disabled_does_not_approve_task() -> None:
    cfg = config()
    cfg = cfg.model_copy(update={"auto_approve": cfg.auto_approve.model_copy(update={"enabled": False})})
    original = task()

    approved, report = apply_one(original, cfg)

    assert approved.approved is False
    assert approved == original
    assert report["enabled"] is False


def test_low_risk_docs_task_on_readme_is_approved() -> None:
    approved, report = apply_one(task())

    assert approved.approved is True
    assert approved.metadata["auto_approval"]["approved_by_policy"] is True
    assert report["approved_tasks"] == ["docs-readme"]


def test_low_risk_tests_task_in_tests_directory_is_approved() -> None:
    approved, _ = apply_one(
        task(
            id="add-tests",
            task_type=TaskType.TESTS,
            risk_score=3,
            affected_files=["tests/test_example.py"],
            test_requirements=["python -m pytest tests/test_example.py"],
        )
    )

    assert approved.approved is True


def test_task_over_risk_limit_is_rejected() -> None:
    approved, report = apply_one(task(risk_score=4))

    assert approved.approved is False
    assert report["rejected_tasks"][0]["reasons"] == ["risk_score_above_limit"]


def test_high_or_critical_risk_is_rejected() -> None:
    approved, report = apply_one(task(risk_level=RiskLevel.HIGH))

    assert approved.approved is False
    assert "risk_level_too_high" in report["rejected_tasks"][0]["reasons"]


def test_blocked_path_is_rejected() -> None:
    approved, report = apply_one(task(affected_files=["deploy/app.yaml"]), config(allowed_paths=["deploy/**"]))

    assert approved.approved is False
    assert "blocked_path" in report["rejected_tasks"][0]["reasons"]


def test_path_outside_allowed_paths_is_rejected() -> None:
    approved, report = apply_one(task(affected_files=["src/app.py"]))

    assert approved.approved is False
    assert "path_not_allowed" in report["rejected_tasks"][0]["reasons"]


def test_task_without_affected_files_is_rejected() -> None:
    approved, report = apply_one(task(affected_files=[]))

    assert approved.approved is False
    assert "missing_affected_files" in report["rejected_tasks"][0]["reasons"]


def test_code_task_without_tests_is_rejected_when_required() -> None:
    approved, report = apply_one(
        task(
            id="bug",
            task_type=TaskType.BUGFIX,
            risk_score=2,
            affected_files=["src/app.py"],
            test_requirements=[],
        ),
        config(allowed_task_types=["bugfix"], allowed_paths=["src/**"]),
    )

    assert approved.approved is False
    assert "missing_test_requirements_for_code" in report["rejected_tasks"][0]["reasons"]


def test_multiple_approved_tasks_are_selected_deterministically() -> None:
    plan = TaskPlan(
        tasks=[
            task(id="tests-z", task_type=TaskType.TESTS, risk_score=3, affected_files=["tests/test_z.py"]),
            task(id="docs-b", task_type=TaskType.DOCS, risk_score=1, affected_files=["docs/b.md"]),
            task(id="docs-a", task_type=TaskType.DOCS, risk_score=1, affected_files=["docs/a.md"]),
        ]
    )

    approved_plan, report = AutoApprovalPolicy(config()).apply(plan)

    assert [item.id for item in approved_plan.tasks if item.approved] == ["tests-z", "docs-b", "docs-a"]
    assert report["selected_task_id"] == "docs-a"
