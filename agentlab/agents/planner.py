from __future__ import annotations

import json
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import AgentTask, ArchitectureSummary, RepoIndex, RiskLevel, TaskPlan, TaskType
from agentlab.policies.risk import assess_risk
from agentlab.rust_crate import RustCrateLayout, rust_crate_layout
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
                plan = self.ollama.chat_json(
                    model=self.config.agent_model("planner"),
                    system_prompt=load_prompt("planner.md"),
                    user_prompt=json.dumps(context, indent=2),
                    response_model=TaskPlan,
                )
                return self._normalize_plan(plan, context)
            except Exception:
                pass
        return self._normalize_plan(self._heuristic_plan(context), context)

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
            affected_files = _rust_test_baseline_files(context)
            task = AgentTask(
                id="add-test-baseline",
                title="Add a minimal automated test baseline",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                description=f"Create the smallest useful test baseline for the detected project type. Architecture context: {architecture}",
                acceptance_criteria=["A test command can run locally.", "At least one meaningful smoke test exists."],
                affected_files=affected_files,
                forbidden_actions=[
                    "Do not change production behavior.",
                    "Do not edit production source files for a smoke-test baseline unless explicitly asked for inline unit tests or production hooks.",
                ],
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

    def _normalize_plan(self, plan: TaskPlan, context: dict[str, object]) -> TaskPlan:
        tasks = [_normalize_rust_smoke_test_task(task, context, self.file_tool) for task in plan.tasks]
        return plan.model_copy(update={"tasks": tasks})


def _normalize_rust_smoke_test_task(task: AgentTask, context: dict[str, object], file_tool: FileTool) -> AgentTask:
    if task.task_type != TaskType.TESTS or not _rust_roots(context, task):
        return task

    production_files = [path for path in task.affected_files if _is_rust_production_source(path)]
    if production_files and _explicitly_allows_rust_production_test_touch(task):
        metadata = {
            **task.metadata,
            "production_test_change": True,
            "propose_only_recommended": True,
            "planning_note": "Production Rust source files were retained because the task explicitly asks for inline unit tests or production test hooks.",
        }
        forbidden_actions = _dedupe_strings(
            [
                *task.forbidden_actions,
                "Keep production-code test hooks minimal and justify them in the MR.",
            ]
        )
        return task.model_copy(
            update={
                "risk_level": RiskLevel.MEDIUM,
                "risk_score": max(task.risk_score, 10),
                "metadata": metadata,
                "forbidden_actions": forbidden_actions,
            }
        )

    if not _is_rust_smoke_test_task(task, context):
        return task

    layout = _primary_rust_layout(context, task, file_tool)
    if layout is not None and layout.is_binary_only and not _explicitly_allows_rust_production_test_touch(task):
        metadata = {
            **task.metadata,
            "planning_note": (
                "Rust smoke/integration test baseline cannot be safely implemented as test-only: "
                f"{layout.root} has src/main.rs but no src/lib.rs or [lib] target, so integration tests cannot import the crate."
            ),
            "implementation_blocked_reason": "rust_library_seam_required",
            "requires_public_library_seam": True,
            "rust_crate_layout": _layout_metadata(layout),
        }
        forbidden_actions = _dedupe_strings(
            [
                *task.forbidden_actions,
                "Do not create a Rust integration test that imports the package crate unless src/lib.rs or a [lib] target exists.",
                "Do not edit rust-backend/src/*.rs for a smoke-test baseline unless explicitly asked for inline unit tests or a public library seam.",
            ]
        )
        return task.model_copy(
            update={
                "affected_files": [],
                "risk_level": RiskLevel.MEDIUM,
                "risk_score": max(task.risk_score, 10),
                "forbidden_actions": forbidden_actions,
                "metadata": metadata,
            }
        )

    preferred_files = _rust_test_baseline_files(context, task=task, file_tool=file_tool)
    if not preferred_files:
        return task

    forbidden_actions = _dedupe_strings(
        [
            *task.forbidden_actions,
            "Do not edit rust-backend/src/*.rs for a smoke-test baseline unless explicitly asked for inline unit tests or production-code hooks.",
        ]
    )
    metadata = {
        **task.metadata,
        "planning_note": "Rust smoke/integration test baseline affected_files were constrained to test-only files.",
        "removed_production_files": production_files,
        "rust_crate_layout": _layout_metadata(layout) if layout is not None else None,
    }
    planned_production_files = [path for path in preferred_files if _is_rust_production_source(path)]
    if planned_production_files:
        metadata["production_test_change"] = True
        metadata["propose_only_recommended"] = True
        metadata["planning_note"] = "Rust smoke-test baseline requires an explicit public library seam plus integration test."
    return task.model_copy(
        update={
            "affected_files": preferred_files,
            "risk_level": RiskLevel.MEDIUM if planned_production_files else RiskLevel.LOW,
            "risk_score": max(task.risk_score, 10) if planned_production_files else min(task.risk_score or 3, 3),
            "forbidden_actions": forbidden_actions,
            "metadata": metadata,
        }
    )


def _is_rust_smoke_test_task(task: AgentTask, context: dict[str, object]) -> bool:
    text = _task_text(task)
    if not any(term in text for term in ("smoke", "integration", "test baseline", "minimal automated test baseline", "minimal rust", "baseline")):
        return False
    return bool(_rust_roots(context, task))


def _rust_test_baseline_files(
    context: dict[str, object],
    *,
    task: AgentTask | None = None,
    file_tool: FileTool | None = None,
) -> list[str]:
    roots = _rust_roots(context, task)
    if not roots:
        return []
    root = roots[0]
    base = f"{root}/" if root != "." else ""
    if task is not None and file_tool is not None:
        layout = _primary_rust_layout(context, task, file_tool)
        if layout is not None and layout.is_binary_only:
            if _explicitly_allows_rust_production_test_touch(task):
                if "inline unit test" in _task_text(task) or "unit tests inside" in _task_text(task):
                    main_path = f"{base}src/main.rs"
                    return [main_path] if main_path in _context_files(context) else []
                return [f"{base}src/lib.rs", f"{base}tests/smoke.rs"]
            return []
    files = [f"{base}tests/smoke.rs"]
    if task is not None and _requires_rust_dev_dependency(task):
        cargo = f"{base}Cargo.toml"
        if cargo not in files:
            files.append(cargo)
    return files


def _rust_roots(context: dict[str, object], task: AgentTask | None = None) -> list[str]:
    candidates: list[str] = []
    for key in ("manifests", "files"):
        values = context.get(key)
        if isinstance(values, list):
            candidates.extend(str(value).replace("\\", "/") for value in values)
    if task is not None:
        candidates.extend(path.replace("\\", "/") for path in task.affected_files)
    roots: list[str] = []
    for path in candidates:
        if path.endswith("Cargo.toml"):
            root = path.removesuffix("/Cargo.toml") if "/" in path else "."
        elif path.startswith("rust-backend/"):
            root = "rust-backend"
        else:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _is_rust_production_source(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("rust-backend/src/") and normalized.endswith(".rs")


def _explicitly_allows_rust_production_test_touch(task: AgentTask) -> bool:
    text = _task_text(task)
    return any(
        term in text
        for term in (
            "inline unit test",
            "inline unit tests",
            "unit tests inside",
            "production hook",
            "production-code hook",
            "test hook",
            "public seam",
            "library seam",
            "src/lib.rs",
            "lib.rs",
        )
    )


def _requires_rust_dev_dependency(task: AgentTask) -> bool:
    text = _task_text(task)
    return any(term in text for term in ("dev-dependency", "dev-dependencies", "dev dependency", "test dependency", "add dependency"))


def _task_text(task: AgentTask) -> str:
    return " ".join(
        [
            task.id,
            task.title,
            task.description,
            " ".join(task.acceptance_criteria),
            " ".join(task.affected_files),
            " ".join(task.test_requirements),
            json.dumps(task.metadata, ensure_ascii=False, default=str),
        ]
    ).lower()


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _primary_rust_layout(context: dict[str, object], task: AgentTask | None, file_tool: FileTool) -> RustCrateLayout | None:
    roots = _rust_roots(context, task)
    if not roots:
        return None
    files = _context_files(context)
    root = roots[0]
    return rust_crate_layout(root, files, file_tool.read_file)


def _context_files(context: dict[str, object]) -> set[str]:
    files: set[str] = set()
    for key in ("files", "manifests", "test_files"):
        values = context.get(key)
        if isinstance(values, list):
            files.update(str(value).replace("\\", "/") for value in values)
    return files


def _layout_metadata(layout: RustCrateLayout) -> dict[str, object]:
    return {
        "root": layout.root,
        "package_name": layout.package_name,
        "import_name": layout.import_name,
        "has_library": layout.has_library,
        "has_lib_rs": layout.has_lib_rs,
        "has_lib_section": layout.has_lib_section,
        "has_main_rs": layout.has_main_rs,
        "source_files": list(layout.source_files),
    }
