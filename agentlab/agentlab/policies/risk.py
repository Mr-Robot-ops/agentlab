from __future__ import annotations

import re
from pathlib import PurePosixPath

from agentlab.models import AgentTask, RiskAssessment, RiskLevel, TaskType


DOC_PATTERNS = (".md", ".rst", ".txt", ".adoc")
TEST_PARTS = ("test", "tests", "__tests__", "spec")
AUTH_PARTS = ("auth", "oauth", "login", "jwt", "session", "permission", "acl", "rbac")
DB_PARTS = ("migration", "migrations", "schema", "alembic", "flyway", "liquibase")
CI_PARTS = (".gitlab-ci.yml", ".github", "jenkinsfile", "azure-pipelines", "circleci")
INFRA_PARTS = ("dockerfile", "docker-compose", "helm", "terraform", "k8s", "kubernetes", "ansible")
DEPENDENCY_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
)
SECRET_PATH_PARTS = (".env", "id_rsa", "id_dsa", "secret", "secrets", "private_key", ".p12", ".pem")
SECRET_CONTENT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"aws_secret_access_key\s*=",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"(password|token|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"]+['\"]",
    )
]


def _path(value: str) -> PurePosixPath:
    return PurePosixPath(value.replace("\\", "/").lower())


def _contains_part(path: PurePosixPath, candidates: tuple[str, ...]) -> bool:
    parts = set(path.parts)
    path_text = str(path)
    return any(candidate in parts or candidate in path_text for candidate in candidates)


def is_docs_only(paths: list[str]) -> bool:
    return bool(paths) and all(_path(path).suffix in DOC_PATTERNS for path in paths)


def is_tests_only(paths: list[str]) -> bool:
    return bool(paths) and all(_contains_part(_path(path), TEST_PARTS) for path in paths)


def detect_secret_paths(paths: list[str]) -> list[str]:
    touched: list[str] = []
    for path in paths:
        normalized = str(_path(path))
        if any(part in normalized for part in SECRET_PATH_PARTS):
            touched.append(path)
    return touched


def detect_secret_content(diff_text: str) -> bool:
    return any(pattern.search(diff_text) for pattern in SECRET_CONTENT_PATTERNS)


def risk_level(score: int, blocked: bool = False) -> RiskLevel:
    if blocked or score >= 80:
        return RiskLevel.CRITICAL
    if score >= 50:
        return RiskLevel.HIGH
    if score >= 20:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def assess_risk(task: AgentTask, changed_files: list[str], diff_text: str = "") -> RiskAssessment:
    paths = [_path(path) for path in changed_files]
    reasons: list[str] = []
    secret_paths = detect_secret_paths(changed_files)
    secrets_touched = bool(secret_paths) or detect_secret_content(diff_text)

    if secrets_touched:
        reasons.append("secrets touched")
        return RiskAssessment(
            score=1000,
            level=RiskLevel.CRITICAL,
            blocked=True,
            reasons=reasons,
            touched_secret_paths=secret_paths,
        )

    if is_docs_only(changed_files):
        score = 1
        reasons.append("docs only")
    elif is_tests_only(changed_files):
        score = 3
        reasons.append("tests only")
    else:
        base_by_type = {
            TaskType.BUGFIX: 10,
            TaskType.FEATURE: 25,
            TaskType.DEPENDENCY: 30,
            TaskType.AUTH: 50,
            TaskType.DATABASE_MIGRATION: 60,
            TaskType.CI: 70,
            TaskType.INFRA: 80,
            TaskType.SECURITY: 50,
            TaskType.REFACTOR: 20,
            TaskType.DOCS: 1,
            TaskType.TESTS: 3,
            TaskType.UNKNOWN: 20,
        }
        score = base_by_type.get(task.task_type, 20)
        reasons.append(f"task type: {task.task_type}")

    if any(_contains_part(path, AUTH_PARTS) for path in paths):
        score += 50
        reasons.append("auth touched")
    if any(_contains_part(path, DB_PARTS) for path in paths):
        score += 60
        reasons.append("database migration touched")
    if any(_contains_part(path, CI_PARTS) for path in paths):
        score += 70
        reasons.append("ci touched")
    if any(_contains_part(path, INFRA_PARTS) for path in paths):
        score += 80
        reasons.append("infra touched")
    if any(path.name in DEPENDENCY_FILES for path in paths):
        score += 30
        reasons.append("dependency manifest touched")

    return RiskAssessment(score=score, level=risk_level(score), blocked=False, reasons=reasons)
