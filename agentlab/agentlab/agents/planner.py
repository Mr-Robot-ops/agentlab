from __future__ import annotations

import json
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.tools.file_tool import FileTool
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


class PlanningAgent:
    name = "planner"

    def __init__(self, config: AppConfig, file_tool: FileTool, ollama: OllamaClient | None = None) -> None:
        self.config = config
        self.file_tool = file_tool
        self.ollama = ollama

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
        files = self.file_tool.list_files()
        readmes: dict[str, str] = {}
        for path in files:
            if Path(path).name.lower().startswith("readme"):
                try:
                    readmes[path] = compact_text(self.file_tool.read_file(path), 8_000)
                except Exception:
                    continue
        todos = self.file_tool.search_text(r"TODO|FIXME|HACK")
        manifests = [path for path in files if Path(path).name in {"pyproject.toml", "package.json", "go.mod", "Cargo.toml"}]
        tests = [path for path in files if "test" in path.lower()]
        return {
            "files": files[:500],
            "readmes": readmes,
            "todos": todos[:100],
            "manifests": manifests,
            "test_files": tests[:200],
        }

    def _heuristic_plan(self, context: dict[str, object]) -> TaskPlan:
        files = list(context.get("files", []))
        todos = list(context.get("todos", []))
        manifests = list(context.get("manifests", []))
        tests = list(context.get("test_files", []))
        tasks: list[AgentTask] = []

        if todos:
            task = AgentTask(
                id="todo-triage",
                title="Triage and resolve visible TODO/FIXME markers",
                task_type=TaskType.BUGFIX,
                risk_level=RiskLevel.MEDIUM,
                description="Inspect TODO/FIXME/HACK markers and implement one small safe fix.",
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
                description="Create the smallest useful test baseline for the detected project type.",
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
