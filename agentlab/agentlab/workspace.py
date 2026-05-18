from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from agentlab.audit import AuditLogger
from agentlab.config import AppConfig
from agentlab.tools.common import ToolError
from agentlab.tools.git_tool import GitTool


class WorkspaceManager:
    def __init__(self, config: AppConfig, audit: AuditLogger | None = None) -> None:
        self.config = config
        self.audit = audit

    def prepare(self) -> dict[str, object]:
        repo_path = self.config.target_repo_path.resolve()
        if (repo_path / ".git").exists():
            return {"status": "existing", "repo_path": str(repo_path)}

        if repo_path.exists() and any(repo_path.iterdir()):
            raise ToolError(f"target_repo_path exists but is not a git checkout: {repo_path}")

        if not self.config.clone_target_repo:
            raise ToolError(
                f"target_repo_path is missing or not a git checkout and clone_target_repo is false: {repo_path}"
            )
        if not self.config.target_repo_url:
            raise ToolError("clone_target_repo is true but target_repo_url is not configured")
        self._validate_repo_url(self.config.target_repo_url)

        repo_path.parent.mkdir(parents=True, exist_ok=True)
        git_tool = GitTool(
            repo_path.parent,
            default_branch=self.config.default_branch,
            timeout_seconds=self.config.command_timeout_seconds,
            audit=self.audit,
        )
        result = git_tool.clone(
            self.config.target_repo_url,
            repo_path,
            branch=self.config.target_repo_ref or self.config.default_branch,
            depth=self.config.clone_depth,
        )
        if not result.ok:
            raise ToolError(result.stderr or "git clone failed")
        return {
            "status": "cloned",
            "repo_path": str(repo_path),
            "ref": self.config.target_repo_ref or self.config.default_branch,
            "depth": self.config.clone_depth,
        }

    def _validate_repo_url(self, repo_url: str) -> None:
        parsed = urlparse(repo_url)
        if parsed.scheme not in {"http", "https", "ssh", "git"} and not repo_url.startswith("git@"):
            raise ToolError("unsupported git repo URL scheme")
        if self.config.reject_embedded_git_credentials and parsed.scheme in {"http", "https"} and "@" in parsed.netloc:
            raise ToolError("embedded git credentials in target_repo_url are not allowed")
