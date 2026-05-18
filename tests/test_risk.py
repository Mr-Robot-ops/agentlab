from agentlab.models import AgentTask, RiskLevel, TaskType
from agentlab.policies.risk import assess_risk


def task(task_type: TaskType = TaskType.BUGFIX) -> AgentTask:
    return AgentTask(id="t1", title="Task", task_type=task_type, approved=True)


def test_docs_only_scores_one() -> None:
    risk = assess_risk(task(TaskType.DOCS), ["README.md", "docs/usage.md"])
    assert risk.score == 1
    assert risk.level == RiskLevel.LOW


def test_tests_only_scores_three() -> None:
    risk = assess_risk(task(TaskType.TESTS), ["tests/test_policy.py"])
    assert risk.score == 3
    assert risk.level == RiskLevel.LOW


def test_bugfix_base_score() -> None:
    risk = assess_risk(task(TaskType.BUGFIX), ["src/app.py"])
    assert risk.score == 10


def test_auth_touch_adds_high_risk() -> None:
    risk = assess_risk(task(TaskType.FEATURE), ["src/auth/session.py"])
    assert risk.score >= 75
    assert risk.level in {RiskLevel.HIGH, RiskLevel.CRITICAL}


def test_dependency_manifest_adds_dependency_risk() -> None:
    risk = assess_risk(task(TaskType.DEPENDENCY), ["pyproject.toml"])
    assert risk.score >= 60


def test_ci_and_infra_are_critical_scale() -> None:
    ci = assess_risk(task(TaskType.CI), [".gitlab-ci.yml"])
    infra = assess_risk(task(TaskType.INFRA), ["Dockerfile"])
    assert ci.score >= 70
    assert infra.score >= 80


def test_secrets_block() -> None:
    risk = assess_risk(task(TaskType.BUGFIX), [".env"], "+TOKEN=\"secret\"\n")
    assert risk.blocked is True
    assert risk.level == RiskLevel.CRITICAL
