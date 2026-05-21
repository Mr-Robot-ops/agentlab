from __future__ import annotations

from pathlib import Path

from agentlab.audit import AuditLogger
from agentlab.models import CommandResult, DiffStats
from agentlab.policies.risk import detect_secret_paths
from agentlab.tools.common import ToolError, run_subprocess


def _safe_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or normalized.startswith("-") or ".." in Path(normalized).parts:
        raise ToolError(f"unsafe git path: {path}")
    return normalized


class GitTool:
    def __init__(
        self,
        repo_path: str | Path,
        *,
        default_branch: str = "main",
        timeout_seconds: int = 300,
        audit: AuditLogger | None = None,
        dry_run: bool = False,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.default_branch = default_branch
        self.timeout_seconds = timeout_seconds
        self.audit = audit
        self.dry_run = dry_run

    def _git(self, args: list[str]) -> CommandResult:
        return run_subprocess(["git", *args], cwd=self.repo_path, timeout_seconds=self.timeout_seconds)

    def clone(
        self,
        repo_url: str,
        destination: str | Path,
        *,
        branch: str | None = None,
        depth: int = 0,
    ) -> CommandResult:
        dest = Path(destination).resolve()
        if dest.exists() and any(dest.iterdir()):
            raise ToolError(f"clone destination already exists: {dest}")
        command = ["git", "clone"]
        if depth > 0:
            command.extend(["--depth", str(depth)])
        if branch:
            command.extend(["--branch", branch])
        command.extend([repo_url, str(dest)])
        return run_subprocess(command, cwd=dest.parent, timeout_seconds=self.timeout_seconds)

    def current_branch(self) -> str:
        result = self._git(["branch", "--show-current"])
        if not result.ok:
            raise ToolError(result.stderr or "could not determine current branch")
        return result.stdout.strip()

    def status_porcelain(self) -> str:
        result = self._git(["status", "--porcelain"])
        if not result.ok:
            raise ToolError(result.stderr or "git status failed")
        return result.stdout.strip()

    def rev_parse(self, ref: str = "HEAD") -> str:
        result = self._git(["rev-parse", ref])
        if not result.ok:
            raise ToolError(result.stderr or f"git rev-parse failed for {ref}")
        return result.stdout.strip()

    def checkout(self, ref: str) -> CommandResult:
        if ref.startswith("-"):
            raise ToolError("unsafe git ref")
        return self._git(["checkout", ref])

    def fetch(self, remote: str = "origin", ref: str | None = None) -> CommandResult:
        if ref and (ref.startswith("-") or ".." in ref):
            raise ToolError("unsafe git ref")
        args = ["fetch", "--prune", remote]
        if ref:
            args.append(ref)
        return self._git(args)

    def checkout_agent_branch(self, branch: str, remote: str = "origin") -> CommandResult:
        if not branch.startswith("agent/") or ".." in branch or branch.endswith(".lock") or branch.startswith("-"):
            raise ToolError("agent branches must match agent/<task-id>")
        fetched = self.fetch(remote, branch)
        if not fetched.ok:
            return fetched
        return self._git(["checkout", "-B", branch, f"{remote}/{branch}"])

    def pull_ff_only(self, remote: str = "origin", branch: str | None = None) -> CommandResult:
        target = branch or self.default_branch
        if target.startswith("-"):
            raise ToolError("unsafe git branch")
        return self._git(["pull", "--ff-only", remote, target])

    def create_branch(self, branch: str, base: str | None = None) -> CommandResult:
        if not branch.startswith("agent/") or ".." in branch or branch.endswith(".lock"):
            raise ToolError("agent branches must match agent/<task-id>")
        args = ["checkout", "-B", branch]
        if base:
            args.append(base)
        return self._git(args)

    def diff(self, base: str = "HEAD") -> str:
        result = self._git(["diff", base])
        if not result.ok:
            raise ToolError(result.stderr or "git diff failed")
        return result.stdout

    def changed_files(self, base: str = "HEAD") -> list[str]:
        result = self._git(["diff", "--name-only", base])
        if not result.ok:
            raise ToolError(result.stderr or "git diff --name-only failed")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def show_file(self, ref: str, path: str) -> str:
        if ref.startswith("-") or ".." in ref:
            raise ToolError("unsafe git ref")
        safe_path = _safe_relative_path(path)
        result = self._git(["show", f"{ref}:{safe_path}"])
        if not result.ok:
            raise ToolError(result.stderr or f"git show failed for {ref}:{safe_path}")
        return result.stdout

    def commit_log(self, base: str, head: str = "HEAD", *, max_count: int = 20) -> list[dict[str, str]]:
        if base.startswith("-") or head.startswith("-") or ".." in base or ".." in head:
            raise ToolError("unsafe git ref")
        result = self._git(
            [
                "log",
                f"--max-count={max_count}",
                "--format=%H%x1f%an%x1f%ae%x1f%s",
                f"{base}..{head}",
            ]
        )
        if not result.ok:
            raise ToolError(result.stderr or "git log failed")
        commits: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            sha, author_name, author_email, subject = parts
            commits.append(
                {
                    "sha": sha,
                    "author_name": author_name,
                    "author_email": author_email,
                    "subject": subject,
                }
            )
        return commits

    def diff_stats(self, base: str = "HEAD", protected_paths: list[str] | None = None) -> DiffStats:
        result = self._git(["diff", "--numstat", base])
        if not result.ok:
            raise ToolError(result.stderr or "git diff --numstat failed")
        changed: list[str] = []
        added = 0
        deleted = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add_raw, del_raw, path = parts
            changed.append(path)
            added += int(add_raw) if add_raw.isdigit() else 0
            deleted += int(del_raw) if del_raw.isdigit() else 0
        protected = [
            path
            for path in changed
            if any(path.replace("\\", "/").startswith(item.rstrip("/") + "/") or path == item for item in protected_paths or [])
        ]
        return DiffStats(
            changed_files=changed,
            added_lines=added,
            deleted_lines=deleted,
            touched_protected_paths=protected,
            secrets_touched=bool(detect_secret_paths(changed)),
        )

    def commit(self, message: str) -> str | None:
        if self.current_branch() == self.default_branch:
            raise ToolError("refusing to commit on default branch")
        return self._commit(message)

    def commit_direct_main(self, message: str) -> str | None:
        if self.current_branch() != self.default_branch:
            raise ToolError("direct main commit must run on default branch")
        return self._commit(message)

    def _commit(self, message: str) -> str | None:
        if self.dry_run:
            return None
        add = self._git(["add", "-A"])
        if not add.ok:
            raise ToolError(add.stderr or "git add failed")
        pending = self._git(["diff", "--cached", "--quiet"])
        if pending.exit_code == 0:
            return None
        commit = self._git(["commit", "-m", message])
        if not commit.ok:
            raise ToolError(commit.stderr or "git commit failed")
        rev = self._git(["rev-parse", "HEAD"])
        if not rev.ok:
            raise ToolError(rev.stderr or "git rev-parse failed")
        return rev.stdout.strip()

    def push(self, branch: str, remote: str = "origin") -> CommandResult:
        if self.dry_run:
            return CommandResult(command="git push (dry-run)", cwd=str(self.repo_path), exit_code=0)
        if branch == self.default_branch:
            raise ToolError("direct default-branch push is not allowed through GitTool.push")
        if not branch.startswith("agent/"):
            raise ToolError("only agent branches can be pushed")
        return self._git(["push", remote, branch])

    def push_default_branch(self, remote: str = "origin") -> CommandResult:
        if self.dry_run:
            return CommandResult(command="git push default branch (dry-run)", cwd=str(self.repo_path), exit_code=0)
        if self.current_branch() != self.default_branch:
            raise ToolError("default branch push must run on default branch")
        return self._git(["push", remote, self.default_branch])

    def cherry_pick(self, commit_sha: str, *, no_commit: bool = False, allow_default_branch: bool = False) -> CommandResult:
        if self.current_branch() == self.default_branch and not allow_default_branch:
            raise ToolError("refusing to cherry-pick on default branch")
        args = ["cherry-pick"]
        if no_commit:
            args.append("--no-commit")
        args.append(commit_sha)
        return self._git(args)

    def revert(self, commit_sha: str, *, no_commit: bool = False) -> CommandResult:
        if self.current_branch() == self.default_branch:
            raise ToolError("refusing to revert on default branch")
        args = ["revert"]
        if no_commit:
            args.append("--no-commit")
        args.append(commit_sha)
        return self._git(args)
