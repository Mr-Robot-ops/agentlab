from __future__ import annotations

from agentlab.models import AgentTask, ArchitectureSummary, BacklogItem, RepoIndex, RiskLevel, StewardReport, TaskType
from agentlab.policies.risk import assess_risk


class BacklogSteward:
    def __init__(self, index: RepoIndex, architecture: ArchitectureSummary) -> None:
        self.index = index
        self.architecture = architecture

    def build_report(self) -> StewardReport:
        backlog: list[BacklogItem] = []
        backlog.extend(self._todo_items())
        backlog.extend(self._test_baseline_items())
        backlog.extend(self._docs_items())
        backlog.extend(self._container_items())

        seen: set[str] = set()
        unique: list[BacklogItem] = []
        for item in backlog:
            if item.id in seen:
                continue
            seen.add(item.id)
            unique.append(item)

        health = self._health_score(unique)
        recommended = [item.id for item in sorted(unique, key=self._priority_rank, reverse=True)[:5]]
        risks = list(self.architecture.risks)
        risks.extend(self.index.warnings)
        return StewardReport(
            summary=f"Indexed {self.index.indexed_files} files across {len(self.index.languages)} languages.",
            repo_health_score=health,
            backlog=unique,
            recommended_next_task_ids=recommended,
            risks=risks,
        )

    def _todo_items(self) -> list[BacklogItem]:
        if not self.index.todos:
            return []
        grouped: dict[str, list[str]] = {}
        for todo in self.index.todos[:20]:
            grouped.setdefault(todo.path, []).append(f"{todo.tag}:{todo.line} {todo.text}")
        items = []
        for path, evidence in grouped.items():
            task = AgentTask(
                id=self._safe_id(f"todo-{path}"),
                title=f"Triage TODO markers in {path}",
                task_type=TaskType.BUGFIX,
                risk_level=RiskLevel.MEDIUM,
                description=f"Review TODO/FIXME/HACK markers in {path} and resolve one small actionable item.",
                acceptance_criteria=["One actionable marker is resolved or documented as intentionally deferred."],
                affected_files=[path],
                forbidden_actions=["Do not perform broad refactors.", "Do not change protected paths."],
                test_requirements=["Run the project-specific test command."],
            )
            risk = assess_risk(task, [path])
            task = task.model_copy(update={"risk_score": risk.score, "risk_level": risk.level})
            items.append(
                BacklogItem(
                    id=task.id,
                    title=task.title,
                    task_type=task.task_type,
                    priority="medium",
                    rationale="Repository contains actionable TODO-style maintenance markers.",
                    evidence=evidence,
                    proposed_task=task,
                )
            )
        return items

    def _test_baseline_items(self) -> list[BacklogItem]:
        if self.index.test_files or not self.index.manifests:
            return []
        task = AgentTask(
            id="add-test-baseline",
            title="Add a minimal automated test baseline",
            task_type=TaskType.TESTS,
            risk_level=RiskLevel.LOW,
            risk_score=3,
            description="The repository has project manifests but no detected tests. Add the smallest meaningful test baseline.",
            acceptance_criteria=["A project-appropriate test command runs locally.", "At least one smoke test exists."],
            affected_files=[],
            forbidden_actions=["Do not change production behavior."],
            test_requirements=["Run the new test command."],
        )
        return [
            BacklogItem(
                id=task.id,
                title=task.title,
                task_type=task.task_type,
                priority="high",
                rationale="No automated tests were detected.",
                evidence=self.index.manifests,
                proposed_task=task,
            )
        ]

    def _docs_items(self) -> list[BacklogItem]:
        if self.index.docs_files:
            return []
        task = AgentTask(
            id="add-repository-readme",
            title="Add a repository README",
            task_type=TaskType.DOCS,
            risk_level=RiskLevel.LOW,
            risk_score=1,
            description="Add a concise README describing project purpose, setup, tests, and operations.",
            acceptance_criteria=["README documents local setup.", "README documents test command.", "README describes runtime or deployment hints."],
            affected_files=["README.md"],
            forbidden_actions=["Do not change source code."],
            test_requirements=["No tests required for docs-only task."],
        )
        return [
            BacklogItem(
                id=task.id,
                title=task.title,
                task_type=task.task_type,
                priority="medium",
                rationale="No documentation files were detected.",
                evidence=[],
                proposed_task=task,
            )
        ]

    def _container_items(self) -> list[BacklogItem]:
        if not self.index.docker_files:
            return []
        task = AgentTask(
            id="review-container-hardening",
            title="Review container hardening opportunities",
            task_type=TaskType.INFRA,
            risk_level=RiskLevel.CRITICAL,
            description="Review Docker/Compose configuration for non-root runtime, health checks, and unsafe mounts.",
            acceptance_criteria=["Container security implications are documented.", "Any change remains small and buildable."],
            affected_files=self.index.docker_files[:3],
            forbidden_actions=["Do not add privileged containers.", "Do not add host mounts.", "Do not alter deployment strategy broadly."],
            test_requirements=["Run docker build or equivalent build gate if available."],
        )
        risk = assess_risk(task, task.affected_files)
        task = task.model_copy(update={"risk_score": risk.score, "risk_level": risk.level})
        return [
            BacklogItem(
                id=task.id,
                title=task.title,
                task_type=task.task_type,
                priority="medium",
                rationale="Container configuration exists and should be reviewed before autonomous changes.",
                evidence=self.index.docker_files,
                proposed_task=task,
            )
        ]

    @staticmethod
    def _safe_id(value: str) -> str:
        cleaned = "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-")
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned[:80] or "backlog-item"

    @staticmethod
    def _priority_rank(item: BacklogItem) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[item.priority]

    def _health_score(self, backlog: list[BacklogItem]) -> int:
        score = 100
        if not self.index.test_files:
            score -= 25
        if not self.index.docs_files:
            score -= 15
        if self.index.todos:
            score -= min(20, len(self.index.todos))
        if self.index.warnings:
            score -= min(15, len(self.index.warnings) * 5)
        if any(item.priority == "critical" for item in backlog):
            score -= 10
        return max(0, min(100, score))
