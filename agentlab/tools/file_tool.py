from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath

from agentlab.config import AppConfig
from agentlab.models import DiffStats, PatchProposal
from agentlab.policies.risk import detect_secret_content, detect_secret_paths
from agentlab.tools.common import ToolError, ensure_within, run_subprocess


class PatchApplyError(ToolError):
    def __init__(self, *, command: list[str], stderr: str, patch: str, check: bool) -> None:
        super().__init__(stderr or ("git apply --check failed" if check else "git apply failed"))
        self.command = command
        self.stderr = stderr
        self.patch = patch
        self.check = check


class UnifiedDiffValidationError(ToolError):
    def __init__(self, *, line_number: int, offending_line: str, reason: str = "missing_diff_prefix_in_hunk") -> None:
        super().__init__(f"{reason} at line {line_number}: {offending_line!r}")
        self.line_number = line_number
        self.offending_line = offending_line
        self.reason = reason

    def to_dict(self) -> dict[str, object]:
        return {
            "line_number": self.line_number,
            "offending_line": self.offending_line,
            "reason": self.reason,
        }


def validate_unified_diff_structure(patch: str) -> None:
    in_hunk = False
    for line_number, line in enumerate(patch.splitlines(), start=1):
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if not line or line in {"---", "+++"} or line.startswith("--- ") or line.startswith("+++ "):
            raise UnifiedDiffValidationError(line_number=line_number, offending_line=line)
        if line[0] not in {" ", "+", "-", "\\"}:
            raise UnifiedDiffValidationError(line_number=line_number, offending_line=line)


class FileTool:
    def __init__(self, repo_path: str | Path, config: AppConfig, *, dry_run: bool = False) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.config = config
        self.dry_run = dry_run

    def _safe_path(self, relative_path: str) -> Path:
        rel = PurePosixPath(relative_path.replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ToolError(f"unsafe relative path: {relative_path}")
        return ensure_within(self.repo_path, self.repo_path / Path(*rel.parts))

    def list_files(self, pattern: str = "*") -> list[str]:
        files: list[str] = []
        for path in self.repo_path.rglob(pattern):
            if path.is_file() and ".git" not in path.parts:
                files.append(path.relative_to(self.repo_path).as_posix())
        return sorted(files)

    def read_file(self, relative_path: str, *, max_bytes: int = 200_000) -> str:
        path = self._safe_path(relative_path)
        if path.stat().st_size > max_bytes:
            raise ToolError(f"file too large to read: {relative_path}")
        return path.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str) -> None:
        path = self._safe_path(relative_path)
        self._validate_paths([relative_path])
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def search_text(self, pattern: str) -> list[str]:
        rg = shutil.which("rg")
        if rg:
            result = run_subprocess(
                [rg, "--line-number", "--glob", "!.git", pattern],
                cwd=self.repo_path,
                timeout_seconds=self.config.command_timeout_seconds,
            )
            if result.exit_code in (0, 1):
                return [line for line in result.stdout.splitlines() if line.strip()]
            raise ToolError(result.stderr or "rg failed")

        matches: list[str] = []
        compiled = re.compile(pattern)
        for path in self.repo_path.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if compiled.search(line):
                        matches.append(f"{path.relative_to(self.repo_path).as_posix()}:{line_no}:{line}")
            except UnicodeDecodeError:
                continue
        return matches

    def validate_patch(self, proposal: PatchProposal) -> DiffStats:
        validate_unified_diff_structure(proposal.patch)
        files = self._patch_files(proposal.patch)
        if proposal.affected_files:
            missing = set(files) - set(proposal.affected_files)
            if missing:
                raise ToolError(f"patch touches files not declared in proposal: {sorted(missing)}")
        self._validate_paths(files)
        added = 0
        deleted = 0
        for line in proposal.patch.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
        if len(files) > self.config.max_changed_files:
            raise ToolError("patch exceeds max_changed_files")
        if added > self.config.max_added_lines:
            raise ToolError("patch exceeds max_added_lines")
        if deleted > self.config.max_deleted_lines:
            raise ToolError("patch exceeds max_deleted_lines")
        secret_paths = detect_secret_paths(files)
        secrets_touched = bool(secret_paths) or detect_secret_content(proposal.patch)
        protected = self._protected_touches(files)
        return DiffStats(
            changed_files=files,
            added_lines=added,
            deleted_lines=deleted,
            touched_protected_paths=protected,
            secrets_touched=secrets_touched,
        )

    def apply_patch(self, proposal: PatchProposal) -> DiffStats:
        stats = self.validate_patch(proposal)
        if stats.secrets_touched:
            raise ToolError("refusing to apply patch that touches secrets")
        if stats.touched_protected_paths:
            raise ToolError("refusing to apply patch that touches protected paths")
        if self.dry_run:
            return stats
        check_command = ["git", "apply", "--check", "--whitespace=nowarn", "-"]
        check = run_subprocess(
            check_command,
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
            input_text=proposal.patch,
        )
        if not check.ok:
            raise PatchApplyError(command=check_command, stderr=check.stderr or "git apply --check failed", patch=proposal.patch, check=True)
        apply_command = ["git", "apply", "--whitespace=nowarn", "-"]
        applied = run_subprocess(
            apply_command,
            cwd=self.repo_path,
            timeout_seconds=self.config.command_timeout_seconds,
            input_text=proposal.patch,
        )
        if not applied.ok:
            raise PatchApplyError(command=apply_command, stderr=applied.stderr or "git apply failed", patch=proposal.patch, check=False)
        return stats

    def _validate_paths(self, paths: list[str]) -> None:
        for path in paths:
            safe = self._safe_path(path)
            if os.name != "nt" and safe.is_symlink():
                raise ToolError(f"refusing to touch symlink: {path}")

    def _protected_touches(self, paths: list[str]) -> list[str]:
        touches: list[str] = []
        for path in paths:
            normalized = path.replace("\\", "/")
            for protected in self.config.protected_paths:
                item = protected.rstrip("/")
                if normalized == item or normalized.startswith(item + "/"):
                    touches.append(path)
        return touches

    def _patch_files(self, patch: str) -> list[str]:
        files: list[str] = []
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    target = parts[3]
                    files.append(target[2:] if target.startswith("b/") else target)
            elif line.startswith("+++ b/"):
                files.append(line[6:].strip())
        unique: list[str] = []
        for file_path in files:
            if file_path != "/dev/null" and file_path not in unique:
                unique.append(file_path)
        if not unique:
            raise ToolError("patch does not declare changed files")
        return unique
