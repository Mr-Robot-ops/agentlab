from __future__ import annotations

import re
import hashlib
import shutil
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from agentlab.config import AppConfig
from agentlab.models import DiffStats, StructuredEditProposal, PatchProposal
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


class StructuredEditError(ToolError):
    def __init__(
        self,
        *,
        reason: str,
        message: str,
        edit_index: int,
        path: str,
        operation: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.edit_index = edit_index
        self.path = path
        self.operation = operation
        self.details = details or {}

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "error": str(self),
            "failing_edit_index": self.edit_index,
            "path": self.path,
            "operation": self.operation,
            **self.details,
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

    def apply_structured_edits(self, proposal: StructuredEditProposal) -> DiffStats:
        planned: dict[str, tuple[Path, str, str]] = {}
        working: dict[str, str] = {}
        originals: dict[str, str] = {}
        paths: dict[str, Path] = {}
        for index, edit in enumerate(proposal.edits):
            path = self._safe_path(edit.path)
            self._validate_paths([edit.path])
            if edit.path not in working:
                originals[edit.path] = path.read_text(encoding="utf-8") if path.exists() else ""
                working[edit.path] = originals[edit.path]
                paths[edit.path] = path
            old_content = working[edit.path]
            if edit.operation == "replace_file":
                new_content = edit.content or ""
            elif edit.operation == "append_to_file":
                if not path.exists():
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="target_file_missing",
                        message=f"append_to_file target does not exist: {edit.path}",
                        target_text=old_content,
                    )
                new_content = old_content + (edit.content or "")
            elif edit.operation == "replace_text":
                if not path.exists():
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="target_file_missing",
                        message=f"replace_text target does not exist: {edit.path}",
                        target_text=old_content,
                    )
                old_text = edit.old_text or ""
                count = old_content.count(old_text)
                if count == 0:
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="old_text_not_found",
                        message=f"replace_text old_text not found in {edit.path}",
                        target_text=old_content,
                    )
                if count > 1:
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="old_text_not_unique",
                        message=f"replace_text old_text occurs multiple times in {edit.path}",
                        target_text=old_content,
                    )
                new_content = old_content.replace(old_text, edit.new_text or "", 1)
            elif edit.operation in {"insert_before", "insert_after"}:
                if not path.exists():
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="target_file_missing",
                        message=f"{edit.operation} target does not exist: {edit.path}",
                        target_text=old_content,
                    )
                anchor = edit.anchor or ""
                count = old_content.count(anchor)
                if count == 0:
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="anchor_not_found",
                        message=f"{edit.operation} anchor not found in {edit.path}",
                        target_text=old_content,
                    )
                if count > 1:
                    raise self._structured_error(
                        edit=edit,
                        edit_index=index,
                        reason="anchor_not_unique",
                        message=f"{edit.operation} anchor occurs multiple times in {edit.path}",
                        target_text=old_content,
                    )
                insert_at = old_content.index(anchor)
                if edit.operation == "insert_after":
                    insert_at += len(anchor)
                new_content = old_content[:insert_at] + (edit.content or "") + old_content[insert_at:]
            else:  # pragma: no cover - pydantic validates operation
                raise ToolError(f"unsupported structured edit operation: {edit.operation}")
            working[edit.path] = new_content

        for relative_path, new_content in working.items():
            planned[relative_path] = (paths[relative_path], originals[relative_path], new_content)

        changed_files = [path for path, (_, old, new) in planned.items() if old != new]
        protected = self._protected_touches(changed_files)
        secret_paths = detect_secret_paths(changed_files)
        secrets_touched = bool(secret_paths) or any(detect_secret_content(new) for _, _, new in planned.values())
        stats = self._structured_diff_stats(planned, protected=protected, secrets_touched=secrets_touched)
        if stats.secrets_touched:
            raise ToolError("refusing to apply structured edit that touches secrets")
        if stats.touched_protected_paths:
            raise ToolError("refusing to apply structured edit that touches protected paths")
        if self.dry_run:
            return stats

        for path, _, new_content in planned.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content, encoding="utf-8")

        check = run_subprocess(["git", "diff", "--check"], cwd=self.repo_path, timeout_seconds=self.config.command_timeout_seconds)
        if not check.ok:
            raise ToolError(check.stderr or "git diff --check failed after structured edit")
        return stats

    def _structured_error(
        self,
        *,
        edit: object,
        edit_index: int,
        reason: str,
        message: str,
        target_text: str,
    ) -> StructuredEditError:
        path = getattr(edit, "path")
        operation = getattr(edit, "operation")
        old_text = getattr(edit, "old_text", None)
        anchor = getattr(edit, "anchor", None)
        target_path = self._safe_path(path)
        details = {
            "old_text_excerpt": _excerpt(old_text),
            "old_text_repr_excerpt": _repr_excerpt(old_text),
            "old_text_sha256": _sha256(old_text),
            "anchor_excerpt": _excerpt(anchor),
            "anchor_repr_excerpt": _repr_excerpt(anchor),
            "anchor_sha256": _sha256(anchor),
            "file_sha256": _sha256(target_text),
            "target_file_exists": target_path.exists(),
            "target_file_size": target_path.stat().st_size if target_path.exists() else 0,
            "candidate_contexts": _candidate_contexts(target_text, old_text or anchor or ""),
        }
        return StructuredEditError(
            reason=reason,
            message=message,
            edit_index=edit_index,
            path=path,
            operation=operation,
            details=details,
        )

    def _validate_paths(self, paths: list[str]) -> None:
        for path in paths:
            safe = self._safe_path(path)
            if safe.is_symlink():
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

    @staticmethod
    def _structured_diff_stats(
        planned: dict[str, tuple[Path, str, str]],
        *,
        protected: list[str],
        secrets_touched: bool,
    ) -> DiffStats:
        changed: list[str] = []
        added = 0
        deleted = 0
        for relative_path, (_, old, new) in planned.items():
            if old == new:
                continue
            changed.append(relative_path)
            old_lines = old.splitlines()
            new_lines = new.splitlines()
            matcher = SequenceMatcher(a=old_lines, b=new_lines)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag in {"replace", "delete"}:
                    deleted += i2 - i1
                if tag in {"replace", "insert"}:
                    added += j2 - j1
        return DiffStats(
            changed_files=changed,
            added_lines=added,
            deleted_lines=deleted,
            touched_protected_paths=protected,
            secrets_touched=secrets_touched,
        )


def _sha256(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _excerpt(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _repr_excerpt(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    return ascii(value[:limit])


def _candidate_contexts(text: str, needle: str, *, limit: int = 500) -> list[str]:
    candidates: list[str] = []
    headings = [line.strip() for line in needle.splitlines() if line.lstrip().startswith("#")]
    search_terms = headings or [part.strip() for part in re.split(r"\s+", needle) if len(part.strip()) >= 8][:5]
    for term in search_terms:
        index = text.find(term)
        if index < 0:
            continue
        start = max(0, index - limit // 2)
        end = min(len(text), index + len(term) + limit // 2)
        context = text[start:end]
        if context not in candidates:
            candidates.append(context)
        if len(candidates) >= 3:
            break
    return candidates
