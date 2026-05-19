from __future__ import annotations

import json
import re
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import ArchitectureSummary, RepoFileSummary, RepoIndex, RepoTodo


MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements.lock",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle",
}
CI_NAMES = {".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"}
DOCKER_NAMES = {"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
K8S_HINTS = ("k8s", "kubernetes", "helm", "chart")
INFRA_HINTS = ("terraform", "ansible", "infra", "helm")
CONFIG_SUFFIXES = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".json"}
LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".ps1": "powershell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
}
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK)\b[:\s-]*(.*)", re.IGNORECASE)


class RepoIndexer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.repo_path = config.target_repo_path.resolve()

    def build_index(self) -> RepoIndex:
        files: list[RepoFileSummary] = []
        todos: list[RepoTodo] = []
        total_files = 0
        skipped = 0
        top_level_dirs: set[str] = set()

        for path in sorted(self.repo_path.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(self.repo_path).as_posix()
            total_files += 1
            if self._ignored(rel):
                skipped += 1
                continue
            if len(files) >= self.config.max_index_files:
                skipped += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped += 1
                continue
            if size > self.config.max_index_file_bytes:
                skipped += 1
                continue

            parts = rel.split("/")
            if len(parts) > 1:
                top_level_dirs.add(parts[0])
            summary = self._summarize_file(rel, size)
            files.append(summary)
            if len(todos) < self.config.max_index_todos and summary.role in {"source", "test", "docs", "config"}:
                todos.extend(self._find_todos(path, rel, self.config.max_index_todos - len(todos)))

        languages = Counter(file.language for file in files if file.language != "unknown")
        index = RepoIndex(
            root_path=str(self.repo_path),
            total_files=total_files,
            indexed_files=len(files),
            skipped_files=skipped,
            files=files,
            languages=dict(languages.most_common()),
            top_level_dirs=sorted(top_level_dirs),
            manifests=[file.path for file in files if file.role == "manifest"],
            test_files=[file.path for file in files if file.role == "test"],
            docs_files=[file.path for file in files if file.role == "docs"],
            ci_files=[file.path for file in files if file.role == "ci"],
            docker_files=[file.path for file in files if file.role == "docker"],
            kubernetes_files=[file.path for file in files if file.role == "kubernetes"],
            infra_files=[file.path for file in files if file.role == "infra"],
            config_files=[file.path for file in files if file.role == "config"],
            security_files=[file.path for file in files if file.role == "security"],
            entrypoint_candidates=self._entrypoints(files),
            todos=todos,
            warnings=self._warnings(total_files, skipped, files),
        )
        return index

    def summarize_architecture(self, index: RepoIndex) -> ArchitectureSummary:
        frameworks: list[str] = []
        package_managers: list[str] = []
        manifests = set(index.manifests)

        if "pyproject.toml" in manifests:
            package_managers.append("python/pyproject")
            frameworks.extend(self._python_framework_hints())
        if "package.json" in manifests:
            package_managers.append("node/npm")
            frameworks.extend(self._package_json_framework_hints())
        if "pnpm-lock.yaml" in manifests:
            package_managers.append("node/pnpm")
        if "go.mod" in manifests:
            package_managers.append("go modules")
        if "Cargo.toml" in manifests:
            package_managers.append("cargo")

        primary_languages = list(index.languages.keys())[:5]
        project_type = self._project_type(index, frameworks)
        test_strategy = self._test_strategy(index)
        build_strategy = self._build_strategy(index)
        deployment = []
        if index.docker_files:
            deployment.append("docker")
        if index.kubernetes_files:
            deployment.append("kubernetes")
        if index.ci_files:
            deployment.append("ci")

        risks = []
        if not index.test_files:
            risks.append("no test files detected")
        if index.ci_files:
            risks.append("ci configuration present; changes require extra care")
        if index.docker_files or index.kubernetes_files:
            risks.append("container/deployment configuration present")
        if index.security_files:
            risks.append("secret-like files detected; changes require human review")

        return ArchitectureSummary(
            project_type=project_type,
            primary_languages=primary_languages,
            frameworks=sorted(set(frameworks)),
            package_managers=package_managers,
            test_strategy=test_strategy,
            build_strategy=build_strategy,
            deployment_signals=deployment,
            important_paths=self._important_paths(index),
            boundaries=[
                "Keep task changes small and module-local.",
                "Do not mix infrastructure, CI, auth, database, and application changes in one task.",
                "Prefer existing test and package manager conventions.",
            ],
            risks=risks,
        )

    def _ignored(self, rel: str) -> bool:
        parts = rel.split("/")
        for pattern in self.config.repo_index_ignore:
            if pattern in parts or fnmatch(rel, pattern) or fnmatch(rel, pattern.rstrip("/") + "/*"):
                return True
        return False

    def _summarize_file(self, rel: str, size: int) -> RepoFileSummary:
        path = Path(rel)
        name = path.name
        lower = rel.lower()
        extension = path.suffix
        role = "source"
        if name.lower() in {".env", ".env.example"} or "secret" in lower:
            role = "security"
        elif name in MANIFEST_NAMES:
            role = "manifest"
        elif name in CI_NAMES or ".github/workflows" in lower or ".gitlab-ci" in lower:
            role = "ci"
        elif name in DOCKER_NAMES or name.startswith("Dockerfile"):
            role = "docker"
        elif any(part in lower for part in K8S_HINTS):
            role = "kubernetes"
        elif any(part in lower for part in INFRA_HINTS):
            role = "infra"
        elif extension.lower() in {".md", ".rst", ".adoc", ".txt"}:
            role = "docs"
        elif "test" in lower or "/spec/" in lower or "__tests__" in lower:
            role = "test"
        elif extension.lower() in CONFIG_SUFFIXES:
            role = "config"
        language = LANG_BY_EXT.get(extension.lower(), "unknown")
        return RepoFileSummary(path=rel, size_bytes=size, extension=extension, language=language, role=role)  # type: ignore[arg-type]

    def _find_todos(self, path: Path, rel: str, remaining: int) -> list[RepoTodo]:
        found: list[RepoTodo] = []
        if remaining <= 0:
            return found
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return found
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = TODO_RE.search(line)
            if not match:
                continue
            found.append(
                RepoTodo(
                    path=rel,
                    line=line_no,
                    tag=match.group(1).upper(),  # type: ignore[arg-type]
                    text=match.group(2).strip()[:240],
                )
            )
            if len(found) >= remaining:
                break
        return found

    def _entrypoints(self, files: list[RepoFileSummary]) -> list[str]:
        candidates = []
        names = {"main.py", "app.py", "server.py", "index.js", "index.ts", "main.go", "main.rs"}
        for file in files:
            if Path(file.path).name in names or file.path in {"agentlab/main.py", "src/main.py"}:
                candidates.append(file.path)
        return candidates[:50]

    def _warnings(self, total_files: int, skipped: int, files: list[RepoFileSummary]) -> list[str]:
        warnings = []
        if skipped:
            warnings.append(f"{skipped} files skipped by index limits or ignore rules")
        if total_files > self.config.max_index_files:
            warnings.append("repository exceeds max_index_files; index is partial")
        if not any(file.role == "test" for file in files):
            warnings.append("no tests detected")
        return warnings

    def _python_framework_hints(self) -> list[str]:
        pyproject = self.repo_path / "pyproject.toml"
        if not pyproject.exists():
            return []
        text = pyproject.read_text(encoding="utf-8", errors="ignore").lower()
        hints = []
        for framework in ("django", "fastapi", "flask", "pydantic", "typer", "pytest"):
            if framework in text:
                hints.append(framework)
        return hints

    def _package_json_framework_hints(self) -> list[str]:
        package_json = self.repo_path / "package.json"
        if not package_json.exists():
            return []
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        return [name for name in ("react", "next", "vue", "svelte", "express", "vite", "jest", "vitest") if name in deps]

    @staticmethod
    def _project_type(index: RepoIndex, frameworks: list[str]) -> str:
        if "fastapi" in frameworks or "flask" in frameworks or "django" in frameworks:
            return "python web service"
        if "typer" in frameworks:
            return "python cli"
        if "react" in frameworks or "next" in frameworks or "vue" in frameworks:
            return "frontend application"
        if "go" in index.languages:
            return "go application"
        if "rust" in index.languages:
            return "rust application"
        if "python" in index.languages:
            return "python project"
        return "unknown"

    @staticmethod
    def _test_strategy(index: RepoIndex) -> str:
        if any(path.endswith(".py") for path in index.test_files):
            return "python tests detected, likely pytest-compatible"
        if any(path.endswith((".js", ".ts", ".tsx", ".jsx")) for path in index.test_files):
            return "node/javascript tests detected"
        if index.test_files:
            return "test files detected"
        return "no automated tests detected"

    @staticmethod
    def _build_strategy(index: RepoIndex) -> str:
        if index.docker_files:
            return "docker build or compose available"
        if "pyproject.toml" in index.manifests:
            return "python package build/test workflow"
        if "package.json" in index.manifests:
            return "node package workflow"
        return "unknown"

    @staticmethod
    def _important_paths(index: RepoIndex) -> list[str]:
        paths = []
        for group in (
            index.manifests,
            index.ci_files,
            index.docker_files,
            index.kubernetes_files,
            index.security_files,
            index.entrypoint_candidates,
        ):
            for path in group:
                if path not in paths:
                    paths.append(path)
        return paths[:100]
