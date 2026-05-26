from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agentlab.models import ReportStatus, TestQualityFinding, TestQualityReport
from agentlab.tools.file_tool import FileTool


TEST_QUALITY_PATH_PREFIXES = ("tests/", "rust-backend/tests/")
PLACEHOLDER_PATTERNS = (
    (re.compile(r"\bassert!\s*\(\s*true\s*(?:,|\))"), "assert_true"),
    (re.compile(r"\bassert_eq!\s*\(\s*1\s*,\s*1\s*(?:,|\))"), "literal_assertion"),
    (re.compile(r"\bassert_eq!\s*\(\s*2\s*\+\s*2\s*,\s*4\s*(?:,|\))"), "literal_assertion"),
    (re.compile(r"\bassert_eq!\s*\(\s*4\s*,\s*2\s*\+\s*2\s*(?:,|\))"), "literal_assertion"),
    (re.compile(r"\bassert_ne!\s*\(\s*0\s*,\s*1\s*(?:,|\))"), "literal_assertion"),
)
RUST_ASSERTION_RE = re.compile(r"\b(?:assert|assert_eq|assert_ne|debug_assert|debug_assert_eq|debug_assert_ne|matches)!\s*\(")
COMMENT_RE = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)
RUST_TEST_MARKER_RE = re.compile(r"#\s*\[\s*(?:[A-Za-z_][A-Za-z0-9_]*::)*test(?:\s*\([^]]*\))?\s*\]")
RUST_FN_RE = re.compile(r"\b(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*(?:->[^{]+)?\{")


class TestQualityError(RuntimeError):
    reason = "placeholder_test_detected"

    def __init__(self, report: TestQualityReport) -> None:
        self.report = report
        first = report.findings[0] if report.findings else None
        detail = f"{first.path}:{first.line or '?'} {first.reason}" if first else self.reason
        super().__init__(f"placeholder test detected: {detail}")


@dataclass(frozen=True)
class RustTestFunction:
    name: str
    body: str
    start_line: int


class TestQualityAgent:
    name = "test_quality"

    def __init__(self, file_tool: FileTool) -> None:
        self.file_tool = file_tool

    def run(self, changed_files: list[str]) -> TestQualityReport:
        test_files = [path for path in changed_files if _is_test_quality_path(path)]
        if not test_files:
            return TestQualityReport(
                status=ReportStatus.SKIPPED,
                passed=True,
                recommendation="No changed test files require placeholder-test analysis.",
            )

        all_files = set(self.file_tool.list_files())
        findings: list[TestQualityFinding] = []
        for path in test_files:
            try:
                content = self.file_tool.read_file(path)
            except Exception as exc:
                findings.append(
                    TestQualityFinding(
                        path=path,
                        line=None,
                        reason="test_file_unreadable",
                        description=str(exc),
                    )
                )
                continue
            if path.endswith(".rs"):
                findings.extend(_rust_findings(path, content, all_files, self.file_tool))
            else:
                findings.extend(_generic_placeholder_findings(path, content))

        if findings:
            return TestQualityReport(
                status=ReportStatus.FAILED,
                passed=False,
                findings=findings,
                reason="placeholder_test_detected",
                recommendation="Replace placeholder tests with assertions over project-specific behavior.",
            )
        return TestQualityReport(
            status=ReportStatus.PASSED,
            passed=True,
            recommendation="Changed tests include project-specific behavior checks.",
        )


def _is_test_quality_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    if lower.endswith((".test.ts", ".test.tsx")):
        return True
    return lower.endswith(".rs") and lower.startswith(TEST_QUALITY_PATH_PREFIXES)


def _rust_findings(
    path: str,
    content: str,
    all_files: set[str],
    file_tool: FileTool,
) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    syntax_findings = _rust_syntax_findings(path, content)
    if syntax_findings:
        return syntax_findings

    markers = _rust_project_markers(path, all_files, file_tool)
    file_has_project_reference = _has_project_reference(content, markers)
    for test in _rust_test_functions(content):
        body_without_comments = COMMENT_RE.sub("", test.body).strip()
        if not body_without_comments:
            findings.append(
                TestQualityFinding(
                    path=path,
                    line=test.start_line,
                    reason="empty_test",
                    description="Rust test body is empty.",
                )
            )
            continue

        for pattern, reason in PLACEHOLDER_PATTERNS:
            match = pattern.search(test.body)
            if match:
                findings.append(
                    TestQualityFinding(
                        path=path,
                        line=_line_for_offset(content, content.find(test.body) + match.start()),
                        reason=reason,
                        description="Rust test uses a placeholder assertion that validates no project behavior.",
                    )
                )

        has_assertion = bool(RUST_ASSERTION_RE.search(test.body))
        has_project_reference = file_has_project_reference or _has_project_reference(test.body, markers)
        if not has_assertion and not has_project_reference:
            findings.append(
                TestQualityFinding(
                    path=path,
                    line=test.start_line,
                    reason="no_assertions_or_project_behavior",
                    description="Rust test has no assertions and no project-specific imports or behavior references.",
                )
            )
        elif not has_project_reference:
            findings.append(
                TestQualityFinding(
                    path=path,
                    line=test.start_line,
                    reason="no_project_behavior",
                    description="Rust test does not reference project-specific modules, routes, functions, APIs, binaries, or crate behavior.",
                )
            )
    return findings


def _rust_test_functions(content: str) -> list[RustTestFunction]:
    tests: list[RustTestFunction] = []
    for marker in RUST_TEST_MARKER_RE.finditer(content):
        fn_match = RUST_FN_RE.search(content, marker.end())
        if not fn_match:
            continue
        open_brace = fn_match.end() - 1
        close_brace = _matching_brace(content, open_brace)
        if close_brace is None:
            continue
        tests.append(
            RustTestFunction(
                name=fn_match.group(1),
                body=content[open_brace + 1 : close_brace],
                start_line=_line_for_offset(content, fn_match.start()),
            )
        )
    return tests


def _rust_syntax_findings(path: str, content: str) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    for marker in RUST_TEST_MARKER_RE.finditer(content):
        fn_match = RUST_FN_RE.search(content, marker.end())
        if not fn_match:
            continue
        open_brace = fn_match.end() - 1
        if _matching_brace(content, open_brace) is None:
            findings.append(
                TestQualityFinding(
                    path=path,
                    line=_line_for_offset(content, fn_match.start()),
                    reason="rust_syntax_incomplete",
                    description="Rust test function has unbalanced braces or is missing a closing brace.",
                )
            )
    return findings


def _matching_brace(content: str, open_brace: int) -> int | None:
    depth = 0
    for index in range(open_brace, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _rust_project_markers(path: str, all_files: set[str], file_tool: FileTool) -> set[str]:
    normalized = path.replace("\\", "/")
    roots = _candidate_rust_roots(normalized, all_files)
    markers: set[str] = {"crate::", "super::", "CARGO_BIN_EXE_"}
    for root in roots:
        cargo_path = f"{root}/Cargo.toml" if root else "Cargo.toml"
        package = _cargo_package_name(cargo_path, file_tool)
        if package:
            markers.add(package)
            markers.add(package.replace("-", "_"))
            markers.add(f"CARGO_BIN_EXE_{package}")
    return {marker for marker in markers if marker}


def _candidate_rust_roots(path: str, all_files: set[str]) -> list[str]:
    roots: list[str] = []
    parts = path.split("/")
    for index in range(len(parts)):
        root = "/".join(parts[:index])
        cargo_path = f"{root}/Cargo.toml" if root else "Cargo.toml"
        if cargo_path in all_files and root not in roots:
            roots.append(root)
    if "rust-backend/Cargo.toml" in all_files and "rust-backend" not in roots:
        roots.append("rust-backend")
    if "Cargo.toml" in all_files and "" not in roots:
        roots.append("")
    return roots


def _cargo_package_name(path: str, file_tool: FileTool) -> str | None:
    try:
        payload = tomllib.loads(file_tool.read_file(path))
    except Exception:
        return None
    package = payload.get("package") if isinstance(payload, dict) else None
    name = package.get("name") if isinstance(package, dict) else None
    return str(name) if name else None


def _has_project_reference(text: str, markers: set[str]) -> bool:
    for marker in markers:
        if marker.endswith("::") or marker.startswith("CARGO_BIN_EXE"):
            if marker in text:
                return True
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?::|!|\b)", text):
            return True
    return False


def _generic_placeholder_findings(path: str, content: str) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    patterns = (
        (re.compile(r"\bassert\s*\(\s*true\s*(?:,|\))"), "assert_true"),
        (re.compile(r"\bexpect\s*\(\s*true\s*\)\s*\.\s*to(?:Be|Equal)\s*\(\s*true\s*\)"), "assert_true"),
        (re.compile(r"\bassert(?:Equal|_eq)?\s*\(\s*1\s*,\s*1\s*(?:,|\))"), "literal_assertion"),
    )
    for pattern, reason in patterns:
        for match in pattern.finditer(content):
            findings.append(
                TestQualityFinding(
                    path=path,
                    line=_line_for_offset(content, match.start()),
                    reason=reason,
                    description="Test uses a placeholder assertion that validates no project behavior.",
                )
            )
    return findings


def _line_for_offset(content: str, offset: int) -> int:
    return content.count("\n", 0, max(0, offset)) + 1
