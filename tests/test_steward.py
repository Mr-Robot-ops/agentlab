from __future__ import annotations

from agentlab.models import ArchitectureSummary, RepoIndex, RepoTodo, TaskType
from agentlab.steward import BacklogSteward


def test_steward_creates_backlog_from_repo_gaps() -> None:
    index = RepoIndex(
        root_path="/repo",
        total_files=3,
        indexed_files=3,
        languages={"python": 1, "toml": 1},
        manifests=["pyproject.toml"],
        docker_files=["Dockerfile"],
        todos=[RepoTodo(path="app.py", line=3, tag="TODO", text="handle timeout")],
        warnings=["no tests detected"],
    )
    architecture = ArchitectureSummary(
        project_type="python project",
        primary_languages=["python"],
        frameworks=["pytest"],
        package_managers=["python/pyproject"],
        test_strategy="no automated tests detected",
        build_strategy="docker build or compose available",
        deployment_signals=["docker"],
        important_paths=["pyproject.toml", "Dockerfile"],
        risks=["no test files detected"],
    )

    report = BacklogSteward(index, architecture).build_report()
    ids = {item.id for item in report.backlog}

    assert "todo-app-py" in ids
    assert "add-test-baseline" in ids
    assert "review-container-hardening" in ids
    assert report.repo_health_score < 100
    assert report.recommended_next_task_ids
    assert any(item.proposed_task.task_type == TaskType.TESTS for item in report.backlog)
