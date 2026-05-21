from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlab.artifacts import ArtifactStore
from agentlab.models import DocsCheckReport, Finding, FindingSeverity, ReportStatus
from agentlab.tools.file_tool import FileTool


README_STRUCTURE_HEADING_RE = re.compile(
    r"^(#{1,6})\s*((?:project|repository|file|directory)\s+structure)\s*:?\s*$",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
HEADING_MISSING_SPACE_RE = re.compile(r"^ {0,3}(#{1,6})([^#\s].*)$")
HEADING_TOO_DEEP_RE = re.compile(r"^ {0,3}#{7,}\s")
ASCII_TREE_MARKERS = ("+--", "|--", "`--", "\\--")
UNICODE_TREE_MARKERS = ("\u251c\u2500\u2500", "\u2514\u2500\u2500")
BROKEN_TREE_MARKERS = ("\ufffd", "\u00e2")


@dataclass(frozen=True)
class FencedBlock:
    path: str
    start_line: int
    info: str
    text: str
    under_structure_heading: bool


class DocsCheckAgent:
    name = "docs_check"

    def __init__(
        self,
        file_tool: FileTool,
        artifacts: ArtifactStore | None = None,
        *,
        content_overrides: dict[str, str] | None = None,
    ) -> None:
        self.file_tool = file_tool
        self.artifacts = artifacts
        self.content_overrides = {path.replace("\\", "/"): content for path, content in (content_overrides or {}).items()}

    def run(self, changed_files: list[str]) -> DocsCheckReport:
        readme_paths = [path for path in _dedupe(changed_files) if is_readme_path(path)]
        if not readme_paths:
            return DocsCheckReport(
                status=ReportStatus.SKIPPED,
                passed=True,
                checks={"docs_check": "skipped", "structure_evidence_check": "skipped"},
                check_statuses={"docs_check": "skipped", "structure_evidence_check": "skipped"},
                docs_check="skipped",
                structure_evidence_check="skipped",
                recommendation="No README files changed.",
            )

        findings: list[Finding] = []
        fenced_blocks: list[FencedBlock] = []
        structure_block_present = False
        for path in readme_paths:
            try:
                content = self.content_overrides.get(path.replace("\\", "/"))
                if content is None:
                    content = self.file_tool.read_file(path)
            except Exception as exc:
                findings.append(
                    _finding(
                        title="README could not be read",
                        path=path,
                        description=str(exc),
                    )
                )
                continue
            findings.extend(_markdown_findings(path, content))
            blocks = _fenced_blocks(path, content)
            fenced_blocks.extend(blocks)
            structure_block_present = structure_block_present or any(block.under_structure_heading for block in blocks)

        findings.extend(_tree_findings(fenced_blocks))
        structure_status, structure_findings = self._structure_evidence_status(structure_block_present)
        findings.extend(structure_findings)

        docs_status = "failed" if findings else "passed"
        checks = {
            "docs_check": docs_status,
            "fenced_code_blocks": "failed" if any(item.title == "Markdown fence is not closed" for item in findings) else "passed",
            "markdown_headings": "failed" if any(item.title.startswith("Malformed Markdown heading") for item in findings) else "passed",
            "tree_blocks": "failed" if any(item.title.startswith("Broken README tree") for item in findings) else "passed",
            "structure_evidence_check": structure_status,
        }
        passed = docs_status == "passed"
        check_statuses = {
            "docs_check": docs_status,
            "structure_evidence_check": structure_status,
        }
        return DocsCheckReport(
            status=ReportStatus.PASSED if passed else ReportStatus.FAILED,
            passed=passed,
            checks=checks,
            check_statuses=check_statuses,
            docs_check=docs_status,
            structure_evidence_check=structure_status,
            findings=findings,
            recommendation=_recommendation(passed=passed, structure_block_present=structure_block_present, structure_status=structure_status),
        )

    def _structure_evidence_status(self, structure_block_present: bool) -> tuple[str, list[Finding]]:
        if not structure_block_present:
            return "skipped", []
        path = self._evidence_path()
        if path is None or not path.exists():
            return "skipped", []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return (
                "failed",
                [
                    _finding(
                        title="Project structure evidence could not be read",
                        description=str(exc),
                    )
                ],
            )
        status = str(payload.get("validation_status") or "").lower()
        removed = payload.get("removed_existing_entries")
        removed_entries = [str(item) for item in removed] if isinstance(removed, list) else []
        if removed_entries:
            return (
                "failed",
                [
                    _finding(
                        title="README project structure removes existing files",
                        description=(
                            "Project structure evidence shows the proposed README tree removed existing files: "
                            + ", ".join(removed_entries)
                        ),
                    )
                ],
            )
        if status == "passed":
            return "passed", []
        if status in {"blocked", "failed"}:
            return (
                "failed",
                [
                    _finding(
                        title="Project structure evidence failed",
                        description=f"validation_status={status}. README Project Structure does not match collected repository evidence.",
                    )
                ],
            )
        return "skipped", []

    def _evidence_path(self) -> Path | None:
        if self.artifacts is None:
            return None
        return self.artifacts.artifacts_dir / "project_structure_evidence.json"


def is_readme_path(path: str) -> bool:
    name = Path(path.replace("\\", "/")).name.lower()
    return name in {"readme.md", "readme.markdown"}


def is_readme_only(paths: list[str]) -> bool:
    return bool(paths) and all(is_readme_path(path) for path in paths)


def _markdown_findings(path: str, content: str) -> list[Finding]:
    findings: list[Finding] = []
    open_fence: tuple[str, int, int] | None = None
    for line_number, line in enumerate(content.splitlines(), start=1):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            char = marker[0]
            if open_fence is None:
                open_fence = (char, len(marker), line_number)
            elif char == open_fence[0] and len(marker) >= open_fence[1]:
                open_fence = None
            continue
        if open_fence is not None:
            continue
        if HEADING_MISSING_SPACE_RE.match(line):
            findings.append(
                _finding(
                    title="Malformed Markdown heading: missing space",
                    path=path,
                    line=line_number,
                    description="ATX headings must have a space after the # marker.",
                )
            )
        elif HEADING_TOO_DEEP_RE.match(line):
            findings.append(
                _finding(
                    title="Malformed Markdown heading: too deep",
                    path=path,
                    line=line_number,
                    description="Markdown headings must use at most six # markers.",
                )
            )
    if open_fence is not None:
        findings.append(
            _finding(
                title="Markdown fence is not closed",
                path=path,
                line=open_fence[2],
                description="A fenced code block was opened but no matching closing fence was found.",
            )
        )
    return findings


def _fenced_blocks(path: str, content: str) -> list[FencedBlock]:
    blocks: list[FencedBlock] = []
    current_heading_is_structure = False
    open_fence: tuple[str, int, int, str, bool, list[str]] | None = None
    for line_number, line in enumerate(content.splitlines(), start=1):
        if open_fence is None:
            if README_STRUCTURE_HEADING_RE.match(line.strip()):
                current_heading_is_structure = True
            elif line.lstrip().startswith("#"):
                current_heading_is_structure = False
            fence = FENCE_RE.match(line)
            if fence:
                marker = fence.group("fence")
                open_fence = (
                    marker[0],
                    len(marker),
                    line_number,
                    fence.group("info").strip(),
                    current_heading_is_structure,
                    [],
                )
            continue

        fence = FENCE_RE.match(line)
        if fence and fence.group("fence")[0] == open_fence[0] and len(fence.group("fence")) >= open_fence[1]:
            _, _, start_line, info, under_structure_heading, lines = open_fence
            blocks.append(
                FencedBlock(
                    path=path,
                    start_line=start_line,
                    info=info,
                    text="\n".join(lines),
                    under_structure_heading=under_structure_heading,
                )
            )
            open_fence = None
            continue
        open_fence[5].append(line)
    return blocks


def _tree_findings(blocks: list[FencedBlock]) -> list[Finding]:
    findings: list[Finding] = []
    for block in blocks:
        if not _is_tree_block(block):
            continue
        for offset, line in enumerate(block.text.splitlines(), start=1):
            line_number = block.start_line + offset
            broken = _broken_tree_line_reason(line)
            if broken:
                findings.append(
                    _finding(
                        title="Broken README tree indentation",
                        path=block.path,
                        line=line_number,
                        description=broken,
                    )
                )
    return findings


def _is_tree_block(block: FencedBlock) -> bool:
    if block.under_structure_heading:
        return True
    lowered = block.info.lower()
    if lowered in {"tree", "text", "txt", "plain"}:
        return any(marker in block.text for marker in (*ASCII_TREE_MARKERS, *UNICODE_TREE_MARKERS))
    return False


def _broken_tree_line_reason(line: str) -> str | None:
    if not line.strip() or line.strip() == ".":
        return None
    if any(marker in line for marker in BROKEN_TREE_MARKERS):
        return "Tree block contains broken connector characters."
    if any(fragment in line for fragment in ("+- ", "|- ", "`- ", "\\- ")):
        return "Tree connector appears truncated; expected a connector like +-- or |--."

    marker_position = _tree_marker_position(line)
    if marker_position is None:
        return None
    prefix = line[:marker_position]
    if marker_position % 4 != 0:
        return "Tree connector indentation should align to four-column levels."
    allowed_prefix_chars = {" ", "|", "`", "\\"}
    if any(char not in allowed_prefix_chars and char != "\u2502" for char in prefix):
        return "Tree connector prefix contains unexpected characters."
    return None


def _tree_marker_position(line: str) -> int | None:
    positions = [line.find(marker) for marker in (*ASCII_TREE_MARKERS, *UNICODE_TREE_MARKERS)]
    positions = [position for position in positions if position >= 0]
    return min(positions) if positions else None


def _finding(*, title: str, path: str | None = None, line: int | None = None, description: str = "") -> Finding:
    return Finding(
        tool="docs_check",
        severity=FindingSeverity.HIGH,
        title=title,
        path=path,
        line=line,
        description=description,
        blocked=True,
    )


def _recommendation(*, passed: bool, structure_block_present: bool, structure_status: str) -> str:
    if not passed:
        return "Fix README documentation issues before merge."
    if structure_block_present and structure_status == "skipped":
        return "Proceed, but generate project_structure_evidence.json for Project Structure changes before relying on semantic completeness."
    return "Proceed"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.replace("\\", "/")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
