from __future__ import annotations

import json
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import AgentTask, ArchitectureSummary, RepoIndex, RiskLevel, TaskPlan, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.tools.file_tool import FileTool
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


class PlanningAgent:
    name = "planner"

    def __init__(
        self,
        config: AppConfig,
        file_tool: FileTool,
        ollama: OllamaClient | None = None,
        *,
        repo_index: RepoIndex | None = None,
        architecture: ArchitectureSummary | None = None,
    ) -> None:
        self.config = config
        self.file_tool = file_tool
        self.ollama = ollama
        self.repo_index = repo_index
        self.architecture = architecture

    def plan(self) -> TaskPlan:
        context = self._repo_context()
        if self.ollama is not None:
            try:
                return self.ollama.chat_json(
                    model=self.config.agent_model("planner"),
                    system_prompt=load_prompt("planner.md"),
                    user_prompt=json.dumps(context, indent=2),
                    response_model=TaskPlan,
                )
            except Exception:
                pass
        return self._heuristic_plan(context)

    def _repo_context(self) -> dict[str, object]:
        files = [file.path for file in self.repo_index.files] if self.repo_index is not None else self.file_tool.list_files()
        readmes: dict[str, str] = {}
        for path in files:
            if Path(path).name.lower().startswith("readme"):
                try:
                    readmes[path] = compact_text(self.file_tool.read_file(path), 8_000)
                except Exception:
                    continue
        if self.repo_index is not None:
            todos = [f"{todo.path}:{todo.line}: {todo.tag} {todo.text}" for todo in self.repo_index.todos]
            manifests = self.repo_index.manifests
            tests = self.repo_index.test_files
        else:
            todos = self.file_tool.search_text(r"TODO|FIXME|HACK")
            manifests = [path for path in files if Path(path).name in {"pyproject.toml", "package.json", "go.mod", "Cargo.toml"}]
            tests = [path for path in files if "test" in path.lower()]
        context: dict[str, object] = {
            "files": files[:500],
            "readmes": readmes,
            "todos": todos[:100],
            "manifests": manifests,
            "test_files": tests[:200],
        }
        if self.repo_index is not None:
            context["repo_index"] = {
                "total_files": self.repo_index.total_files,
                "indexed_files": self.repo_index.indexed_files,
                "languages": self.repo_index.languages,
                "top_level_dirs": self.repo_index.top_level_dirs,
                "docs_files": self.repo_index.docs_files[:100],
                "ci_files": self.repo_index.ci_files,
                "docker_files": self.repo_index.docker_files,
                "kubernetes_files": self.repo_index.kubernetes_files[:100],
                "infra_files": self.repo_index.infra_files[:100],
                "security_files": self.repo_index.security_files[:100],
                "entrypoint_candidates": self.repo_index.entrypoint_candidates,
                "warnings": self.repo_index.warnings,
            }
        if self.architecture is not None:
            context["architecture_summary"] = self.architecture.model_dump(mode="json")
        return context

    def _heuristic_plan(self, context: dict[str, object]) -> TaskPlan:
        files = list(context.get("files", []))
        todos = list(context.get("todos", []))
        manifests = list(context.get("manifests", []))
        tests = list(context.get("test_files", []))
        architecture = context.get("architecture_summary", {})
        tasks: list[AgentTask] = []

        if todos:
            task = AgentTask(
                id="todo-triage",
                title="Triage and resolve visible TODO/FIXME markers",
                task_type=TaskType.BUGFIX,
                risk_level=RiskLevel.MEDIUM,
                description="Inspect TODO/FIXME/HACK markers in the context of the repository architecture and implement one small safe fix.",
                acceptance_criteria=["One actionable marker is addressed with a focused change."],
                affected_files=[str(item).split(":", 1)[0] for item in todos[:5]],
                forbidden_actions=["Do not perform broad refactors.", "Do not change protected paths."],
                test_requirements=["Run the project-specific unit tests."],
            )
            risk = assess_risk(task, task.affected_files)
            tasks.append(task.model_copy(update={"risk_score": risk.score, "risk_level": risk.level}))

        if manifests and not tests:
            task = AgentTask(
                id="add-test-baseline",
                title="Add a minimal automated test baseline",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                description=f"Create the smallest useful test baseline for the detected project type. Architecture context: {architecture}",
                acceptance_criteria=["A test command can run locally.", "At least one meaningful smoke test exists."],
                affected_files=[],
                forbidden_actions=["Do not change production behavior."],
                test_requirements=["Run the new test command."],
            )
            tasks.append(task.model_copy(update={"risk_score": 3, "risk_level": RiskLevel.LOW}))

        if "Dockerfile" in files:
            task = AgentTask(
                id="dockerfile-hardening-review",
                title="Review Dockerfile hardening opportunities",
                task_type=TaskType.INFRA,
                risk_level=RiskLevel.CRITICAL,
                description="Inspect Dockerfile for obvious production hardening gaps.",
                acceptance_criteria=["Docker build still succeeds.", "Security implications are documented."],
                affected_files=["Dockerfile"],
                forbidden_actions=["Do not introduce privileged containers.", "Do not add host mounts."],
                test_requirements=["Run docker build if Docker is available."],
            )
            risk = assess_risk(task, ["Dockerfile"])
            tasks.append(task.model_copy(update={"risk_score": risk.score, "risk_level": risk.level}))

        if not tasks:
            task = AgentTask(
                id="repo-health-review",
                title="Document repository health findings",
                task_type=TaskType.DOCS,
                risk_level=RiskLevel.LOW,
                description="Create a concise repository health note based on current structure and test availability.",
                acceptance_criteria=["Findings are structured and actionable."],
                affected_files=["README.md"] if "README.md" in files else [],
                forbidden_actions=["Do not change source code."],
                test_requirements=["No tests required for documentation-only report."],
            )
            tasks.append(task.model_copy(update={"risk_score": 1}))

        return TaskPlan(summary="Heuristic local plan generated without Ollama.", tasks=tasks, source_signals=["files", "readme", "todos", "tests"])
