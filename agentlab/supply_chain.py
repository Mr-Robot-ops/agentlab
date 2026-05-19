from __future__ import annotations

import json
import re
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from agentlab.config import AppConfig
from agentlab.models import Finding, FindingSeverity, RepoIndex, ReportStatus, SbomComponent, SbomDocument, SupplyChainReport


REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*([=<>!~]{1,2})?\s*([^;\s#]+)?")
GO_REQUIRE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-/]+)\s+(v[^\s]+)")


class SupplyChainAnalyzer:
    def __init__(self, config: AppConfig, index: RepoIndex) -> None:
        self.config = config
        self.index = index
        self.repo_path = config.target_repo_path.resolve()

    def analyze(self) -> SupplyChainReport:
        components = self._components()
        lockfiles = self._lockfiles()
        missing_lockfiles = self._missing_lockfiles(lockfiles)
        findings = self._findings(missing_lockfiles)
        recommendations = self._recommendations(missing_lockfiles, findings)
        sbom = SbomDocument(
            serialNumber=f"urn:uuid:{uuid.uuid4()}",
            metadata={
                "timestamp": datetime.now(UTC).isoformat(),
                "tools": [{"vendor": "AgentLab", "name": "agentlab", "version": "0.1.0"}],
                "component": {"type": "application", "name": self.repo_path.name},
                "properties": [
                    {"name": "agentlab:repository_path", "value": str(self.repo_path)},
                    {"name": "agentlab:indexed_files", "value": str(self.index.indexed_files)},
                ],
            },
            components=components,
        )
        blocking_findings = [finding for finding in findings if finding.blocked]
        return SupplyChainReport(
            status=ReportStatus.FAILED if blocking_findings else ReportStatus.PASSED,
            passed=not blocking_findings,
            manifests=self.index.manifests,
            lockfiles=lockfiles,
            missing_lockfiles=missing_lockfiles,
            package_managers=self._package_managers(),
            components_count=len(components),
            findings=findings,
            recommendations=recommendations,
            sbom=sbom,
        )

    def _components(self) -> list[SbomComponent]:
        components: list[SbomComponent] = []
        components.extend(self._pyproject_components())
        components.extend(self._requirements_components())
        components.extend(self._package_json_components())
        components.extend(self._go_mod_components())
        components.extend(self._cargo_components())
        seen: set[str] = set()
        unique: list[SbomComponent] = []
        for component in components:
            key = component.bom_ref
            if key in seen:
                continue
            seen.add(key)
            unique.append(component)
        return sorted(unique, key=lambda item: item.bom_ref)

    def _pyproject_components(self) -> list[SbomComponent]:
        path = self.repo_path / "pyproject.toml"
        if not path.exists():
            return []
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return []
        project = data.get("project", {})
        dependencies = list(project.get("dependencies", []) or [])
        optional = project.get("optional-dependencies", {}) or {}
        for values in optional.values():
            dependencies.extend(values or [])
        return [self._python_component(raw, "pyproject.toml") for raw in dependencies if isinstance(raw, str)]

    def _requirements_components(self) -> list[SbomComponent]:
        components: list[SbomComponent] = []
        for rel in self.index.manifests:
            if Path(rel).name != "requirements.txt":
                continue
            path = self.repo_path / rel
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "-r", "--")):
                    continue
                components.append(self._python_component(stripped, rel))
        return components

    def _package_json_components(self) -> list[SbomComponent]:
        path = self.repo_path / "package.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        components: list[SbomComponent] = []
        for scope_name, scope in (("dependencies", "required"), ("devDependencies", "optional"), ("peerDependencies", "optional")):
            for name, version in (data.get(scope_name, {}) or {}).items():
                components.append(
                    SbomComponent(
                        bom_ref=f"pkg:npm/{name}@{version}",
                        name=name,
                        version=str(version),
                        purl=f"pkg:npm/{name}@{version}",
                        scope=scope,  # type: ignore[arg-type]
                        properties=[{"name": "agentlab:source_path", "value": "package.json"}],
                    )
                )
        return components

    def _go_mod_components(self) -> list[SbomComponent]:
        path = self.repo_path / "go.mod"
        if not path.exists():
            return []
        components: list[SbomComponent] = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            normalized = line.strip()
            if normalized.startswith("require "):
                normalized = normalized.removeprefix("require ").strip()
            match = GO_REQUIRE_RE.match(normalized)
            if not match or match.group(1) == "require":
                continue
            name, version = match.groups()
            components.append(
                SbomComponent(
                    bom_ref=f"pkg:golang/{name}@{version}",
                    name=name,
                    version=version,
                    purl=f"pkg:golang/{name}@{version}",
                    properties=[{"name": "agentlab:source_path", "value": "go.mod"}],
                )
            )
        return components

    def _cargo_components(self) -> list[SbomComponent]:
        path = self.repo_path / "Cargo.toml"
        if not path.exists():
            return []
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return []
        components: list[SbomComponent] = []
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, raw in (data.get(section, {}) or {}).items():
                version = raw if isinstance(raw, str) else raw.get("version") if isinstance(raw, dict) else None
                components.append(
                    SbomComponent(
                        bom_ref=f"pkg:cargo/{name}@{version or 'unknown'}",
                        name=name,
                        version=str(version) if version else None,
                        purl=f"pkg:cargo/{name}@{version}" if version else None,
                        scope="required" if section == "dependencies" else "optional",
                        properties=[{"name": "agentlab:source_path", "value": "Cargo.toml"}],
                    )
                )
        return components

    @staticmethod
    def _python_component(raw: str, source_path: str) -> SbomComponent:
        match = REQ_RE.match(raw)
        name = match.group(1) if match else raw
        version = match.group(3) if match and match.group(2) == "==" else None
        return SbomComponent(
            bom_ref=f"pkg:pypi/{name}@{version or 'unknown'}",
            name=name,
            version=version,
            purl=f"pkg:pypi/{name}@{version}" if version else f"pkg:pypi/{name}",
            properties=[{"name": "agentlab:source_path", "value": source_path}],
        )

    def _lockfiles(self) -> list[str]:
        names = {
            "poetry.lock",
            "uv.lock",
            "Pipfile.lock",
            "requirements.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "go.sum",
            "Cargo.lock",
        }
        return sorted(path for path in self.index.manifests if Path(path).name in names)

    def _missing_lockfiles(self, lockfiles: list[str]) -> list[str]:
        manifests = set(self.index.manifests)
        locks = {Path(path).name for path in lockfiles}
        missing: list[str] = []
        if "package.json" in manifests and not {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"} & locks:
            missing.append("package.json")
        if "go.mod" in manifests and "go.sum" not in locks:
            missing.append("go.mod")
        if "Cargo.toml" in manifests and "Cargo.lock" not in locks:
            missing.append("Cargo.toml")
        if "pyproject.toml" in manifests and not {"poetry.lock", "uv.lock", "Pipfile.lock", "requirements.lock"} & locks:
            missing.append("pyproject.toml")
        return missing

    def _findings(self, missing_lockfiles: list[str]) -> list[Finding]:
        findings: list[Finding] = []
        for path in missing_lockfiles:
            findings.append(
                Finding(
                    tool="agentlab-supply-chain",
                    severity=FindingSeverity.MEDIUM,
                    title="Dependency manifest has no detected lockfile",
                    path=path,
                    description="Lockfiles improve reproducibility and make dependency changes easier to review.",
                    blocked=self.config.require_lockfiles_for_merge,
                )
            )
        for path in self.index.security_files:
            findings.append(
                Finding(
                    tool="agentlab-supply-chain",
                    severity=FindingSeverity.HIGH,
                    title="Secret-like file detected in repository index",
                    path=path,
                    description="Secret-like files should be reviewed before autonomous changes touch nearby paths.",
                    blocked=False,
                )
            )
        return findings

    @staticmethod
    def _recommendations(missing_lockfiles: list[str], findings: list[Finding]) -> list[str]:
        recommendations = []
        if missing_lockfiles:
            recommendations.append("Add or document lockfile strategy before allowing broad dependency automation.")
        if any(finding.path for finding in findings if finding.title.startswith("Secret-like")):
            recommendations.append("Keep secret-like paths protected and require human review for adjacent changes.")
        if not recommendations:
            recommendations.append("Keep SBOM and provenance artifacts attached to every autonomous run.")
        return recommendations

    def _package_managers(self) -> list[str]:
        managers = []
        manifests = set(self.index.manifests)
        if "pyproject.toml" in manifests or "requirements.txt" in manifests:
            managers.append("python")
        if "package.json" in manifests:
            managers.append("node")
        if "go.mod" in manifests:
            managers.append("go")
        if "Cargo.toml" in manifests:
            managers.append("cargo")
        return managers
