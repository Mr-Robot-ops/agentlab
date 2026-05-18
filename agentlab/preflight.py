from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from agentlab.config import AppConfig
from agentlab.models import PreflightCheck, PreflightReport
from agentlab.tools.common import run_subprocess


WRITE_MODES = {"run-task", "full-flow", "direct-main-flow"}
GITLAB_MODES = {"full-flow", "review-mr", "recover", "direct-main-flow"}


class PreflightChecker:
    def __init__(self, config: AppConfig, *, mode: str) -> None:
        self.config = config
        self.mode = mode
        self.checks: list[PreflightCheck] = []

    def run(self) -> PreflightReport:
        self._check_repo_location()
        self._check_git_available()
        self._check_repo_policy()
        self._check_git_state()
        self._check_token_presence()
        self._check_safety_switches()
        self._check_required_commands()
        passed = not any(check.status == "failed" for check in self.checks)
        return PreflightReport(mode=self.mode, passed=passed, checks=self.checks)

    def _check_repo_location(self) -> None:
        repo_path = self.config.target_repo_path
        if self.config.clone_target_repo:
            if not self.config.target_repo_url:
                self._failed("target_repo_url", "clone_target_repo is true but target_repo_url is missing")
                return
            parsed = urlparse(self.config.target_repo_url)
            if self.config.reject_embedded_git_credentials and parsed.scheme in {"http", "https"} and "@" in parsed.netloc:
                self._failed(
                    "target_repo_url",
                    "target_repo_url contains embedded credentials",
                    "Use Kubernetes Secrets, .netrc, or a Git credential helper instead.",
                )
                return
            self._passed("target_repo_url", "repository clone source is configured")
            return

        if (repo_path / ".git").exists():
            self._passed("target_repo_path", f"existing git checkout found at {repo_path}")
        else:
            self._failed("target_repo_path", f"target_repo_path is not a git checkout: {repo_path}")

    def _check_git_available(self) -> None:
        if shutil.which("git"):
            self._passed("git_available", "git executable is available")
        else:
            self._failed("git_available", "git executable is not available")

    def _check_repo_policy(self) -> None:
        policy_path = self.config.target_repo_path / self.config.repo_policy_file
        if policy_path.exists():
            self._passed("repo_policy", f"repository policy file found: {self.config.repo_policy_file}")
        elif self.config.clone_target_repo and not (self.config.target_repo_path / ".git").exists():
            self._warning(
                "repo_policy",
                f"repository policy file cannot be verified until clone completes: {self.config.repo_policy_file}",
            )
        elif self.config.require_repo_policy_for_write and self.mode in WRITE_MODES:
            self._failed(
                "repo_policy",
                f"repository policy file is required for write mode: {self.config.repo_policy_file}",
                "Add .agentlab.yaml to the target repository or disable require_repo_policy_for_write.",
            )
        else:
            self._warning("repo_policy", f"repository policy file not found: {self.config.repo_policy_file}")

    def _check_git_state(self) -> None:
        if not (self.config.target_repo_path / ".git").exists():
            self._skipped("git_state", "target repository is not present before clone")
            return
        result = run_subprocess(
            ["git", "status", "--porcelain"],
            cwd=self.config.target_repo_path,
            timeout_seconds=min(self.config.command_timeout_seconds, 60),
        )
        if not result.ok:
            self._failed("git_state", result.stderr or "git status failed")
            return
        if result.stdout.strip() and self.mode in WRITE_MODES:
            self._failed(
                "git_state",
                "target repository has uncommitted changes in write mode",
                "Use a clean checkout or run in a fresh Kubernetes job.",
            )
        elif result.stdout.strip():
            self._warning("git_state", "target repository has uncommitted changes")
        else:
            self._passed("git_state", "target repository is clean")

    def _check_token_presence(self) -> None:
        if self.mode not in GITLAB_MODES:
            self._skipped("gitlab_token", "mode does not require GitLab API access")
            return
        if os.environ.get(self.config.gitlab_token_env):
            self._passed("gitlab_token", f"{self.config.gitlab_token_env} is set")
        else:
            self._failed(
                "gitlab_token",
                f"{self.config.gitlab_token_env} is not set",
                "Provide the token through environment variables or a Kubernetes Secret.",
            )

    def _check_safety_switches(self) -> None:
        if self.config.direct_main_push_enabled:
            self._warning("direct_main_push_enabled", "direct main push is enabled")
        else:
            self._passed("direct_main_push_enabled", "direct main push is disabled")
        if self.config.auto_merge_enabled:
            self._warning("auto_merge_enabled", "auto merge is enabled")
        else:
            self._passed("auto_merge_enabled", "auto merge is disabled")

    def _check_required_commands(self) -> None:
        missing = [cmd for cmd in self.config.required_test_commands if cmd not in self.config.allowed_commands]
        if missing:
            self._failed(
                "required_test_commands",
                "required test commands are not allowed: " + ", ".join(missing),
                "Add them to allowed_commands or remove them from the repo policy.",
            )
        elif self.config.required_test_commands:
            self._passed("required_test_commands", "all required test commands are allowlisted")
        else:
            self._skipped("required_test_commands", "no required test commands configured")

    def _passed(self, name: str, message: str) -> None:
        self.checks.append(PreflightCheck(name=name, status="passed", message=message))

    def _warning(self, name: str, message: str, remediation: str | None = None) -> None:
        self.checks.append(PreflightCheck(name=name, status="warning", message=message, remediation=remediation))

    def _failed(self, name: str, message: str, remediation: str | None = None) -> None:
        self.checks.append(PreflightCheck(name=name, status="failed", message=message, remediation=remediation))

    def _skipped(self, name: str, message: str) -> None:
        self.checks.append(PreflightCheck(name=name, status="skipped", message=message))
