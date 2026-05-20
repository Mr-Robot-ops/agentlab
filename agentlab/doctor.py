from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from agentlab.config import AppConfig, gitlab_project_api_id, load_config
from agentlab.models import PreflightCheck
from agentlab.policies.command_policy import CommandPolicy, CommandPolicyError
from agentlab.tools.common import run_subprocess
from agentlab.tools.gitlab_tool import GitLabTool


HttpGet = Callable[..., Any]
Which = Callable[[str], str | None]
RunCommand = Callable[..., Any]
GitLabToolFactory = Callable[[AppConfig], Any]


TOKEN_FIX = """Fix Local/Docker:
  export GITLAB_TOKEN="glpat-..."

Fix Kubernetes:
  kubectl -n agentlab create secret generic agentlab-secrets \\
    --from-literal=GITLAB_TOKEN="glpat-..."
"""


class Doctor:
    def __init__(
        self,
        config_path: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
        http_get: HttpGet | None = None,
        which: Which | None = None,
        run_command: RunCommand | None = None,
        gitlab_tool_factory: GitLabToolFactory | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.environ = environ if environ is not None else os.environ
        self.http_get = http_get or httpx.get
        self.which = which or shutil.which
        self.run_command = run_command or run_subprocess
        self.gitlab_tool_factory = gitlab_tool_factory or GitLabTool
        self.checks: list[PreflightCheck] = []
        self.config: AppConfig | None = None

    def run(self) -> dict[str, Any]:
        self._check_config()
        if self.config is not None:
            self._check_workspace_root()
            self._check_target_repo_source()
            self._check_git()
            self._check_token()
            self._check_git_credential_helper()
            self._check_git_author_identity()
            self._check_gitlab_api()
            self._check_ollama()
            self._check_required_commands()
            self._check_docker()
            self._check_repo_policy()
            self._check_dangerous_flags()
            self._check_auto_approve()
            self._check_schedule()
        exit_code = self.exit_code()
        status = "failed" if exit_code == 2 else "warning" if exit_code == 1 else "passed"
        return {
            "status": status,
            "exit_code": exit_code,
            "config": str(self.config_path),
            "checks": [check.model_dump(mode="json") for check in self.checks],
        }

    def exit_code(self) -> int:
        if any(check.status == "failed" for check in self.checks):
            return 2
        if any(check.status == "warning" for check in self.checks):
            return 1
        return 0

    def _check_config(self) -> None:
        try:
            self.config = load_config(self.config_path)
        except Exception as exc:
            self._failed(
                "config",
                f"config.yaml is not readable or valid: {exc}",
                "Pass --config with a readable AgentLab config.yaml and keep secrets out of that file.",
            )
            return
        self._passed("config", "config.yaml is readable and valid")

    def _check_workspace_root(self) -> None:
        assert self.config is not None
        try:
            self.config.workspace_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._failed(
                "workspace_root",
                f"workspace_root cannot be created: {self.config.workspace_root}: {exc}",
                "Use a writable runs directory or mount the PVC/volume at workspace_root.",
            )
            return
        self._passed("workspace_root", f"workspace_root is writable: {self.config.workspace_root}")

    def _check_target_repo_source(self) -> None:
        assert self.config is not None
        if self.config.target_repo_path.exists():
            self._passed("target_repo_path", f"target_repo_path exists: {self.config.target_repo_path}")
            return
        if self.config.target_repo_url:
            self._passed("target_repo_url", "target_repo_url is configured for clone")
            return
        self._failed(
            "target_repo",
            "target_repo_path is missing and target_repo_url is not set",
            "Set target_repo_url for Kubernetes/Docker clone mode or point target_repo_path at an existing checkout.",
        )

    def _check_git(self) -> None:
        if self.which("git"):
            self._passed("git", "git executable is installed")
        else:
            self._failed("git", "git executable is not installed", "Install Git in the runtime image or local environment.")

    def _check_token(self) -> None:
        assert self.config is not None
        if self.environ.get(self.config.gitlab_token_env):
            self._passed("gitlab_token", f"{self.config.gitlab_token_env} is set")
        else:
            self._failed("gitlab_token", f"{self.config.gitlab_token_env} fehlt", TOKEN_FIX)

    def _check_git_credential_helper(self) -> None:
        helper = self._git_config_value("credential.helper")
        if helper:
            self._passed("git_credential_helper", "git credential.helper is configured")
        else:
            self._warning(
                "git_credential_helper",
                "git credential.helper is not configured",
                "Set a Git credential helper or Kubernetes GIT_CONFIG_* env that reads the password from GITLAB_TOKEN.",
            )

    def _check_git_author_identity(self) -> None:
        missing = []
        if not self._git_config_value("user.name"):
            missing.append("user.name")
        if not self._git_config_value("user.email"):
            missing.append("user.email")
        if not missing:
            self._passed("git_author_identity", "git user.name and user.email are configured")
            return
        message = "git author identity is missing: " + ", ".join(missing)
        remediation = (
            "Set GIT_CONFIG_KEY_N/GIT_CONFIG_VALUE_N for user.name and user.email, or run "
            "`git config user.name \"AgentLab Bot\"` and `git config user.email \"agentlab-bot@example.local\"`."
        )
        if self._write_mode_enabled():
            self._failed("git_author_identity", message, remediation)
        else:
            self._warning("git_author_identity", message, remediation)

    def _git_config_value(self, key: str) -> str | None:
        value = self._git_config_env_value(key)
        if value:
            return value
        if not self.which("git"):
            return None
        assert self.config is not None
        cwd = self.config.target_repo_path if self.config.target_repo_path.exists() else self.config_path.parent
        try:
            result = self.run_command(["git", "config", "--get", key], cwd=cwd, timeout_seconds=30)
        except Exception:
            return None
        if result.ok and result.stdout.strip():
            return result.stdout.strip()
        return None

    def _git_config_env_value(self, key: str) -> str | None:
        try:
            count = int(self.environ.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            return None
        for index in range(count):
            if self.environ.get(f"GIT_CONFIG_KEY_{index}") == key:
                value = self.environ.get(f"GIT_CONFIG_VALUE_{index}", "")
                return value if value.strip() else None
        return None

    def _write_mode_enabled(self) -> bool:
        assert self.config is not None
        return bool(self.config.push_agent_branches_enabled or self.config.direct_main_push_enabled or self.config.auto_merge_enabled)

    def _check_gitlab_api(self) -> None:
        assert self.config is not None
        token = self.environ.get(self.config.gitlab_token_env)
        if not token:
            self._skipped("gitlab_api", "GitLab API check skipped because token is missing")
            return
        project = gitlab_project_api_id(self.config.project_id)
        url = f"{self.config.gitlab_url.rstrip('/')}/api/v4/projects/{project}"
        try:
            response = self.http_get(url, headers={"PRIVATE-TOKEN": token}, timeout=10)
        except Exception as exc:
            self._failed(
                "gitlab_api",
                f"GitLab API is not reachable: {exc}",
                "Check gitlab_url, routing/DNS, TLS trust, and Kubernetes NetworkPolicy/firewall rules.",
            )
            return
        status_code = getattr(response, "status_code", None)
        if status_code == 200:
            self._passed("gitlab_project", "GitLab project is reachable with the configured token")
        elif status_code in {401, 403}:
            self._failed(
                "gitlab_project",
                "GitLab token cannot read the project",
                "Use read_api/read_repository for dry-run, or api/read_repository/write_repository for MR/direct-main test modes.",
            )
        elif status_code == 404:
            self._failed(
                "gitlab_project",
                f"GitLab project was not found for configured project_id={self.config.project_id}",
                'Check project_id and try the numeric GitLab project ID for scheduler reliability. Example: project_id: "5".',
            )
        else:
            self._failed("gitlab_project", f"GitLab API returned HTTP {status_code}", "Inspect GitLab availability and token scopes.")

    def _check_ollama(self) -> None:
        assert self.config is not None
        url = f"{self.config.ollama.base_url.rstrip('/')}/api/tags"
        try:
            response = self.http_get(url, timeout=10)
        except Exception as exc:
            self._failed("ollama_api", f"Ollama API is not reachable: {exc}", "Check ollama.base_url and network reachability.")
            return
        if getattr(response, "status_code", None) != 200:
            self._failed("ollama_api", f"Ollama API returned HTTP {getattr(response, 'status_code', None)}", "Check Ollama service health.")
            return
        self._passed("ollama_api", "Ollama API is reachable")
        model = self.config.ollama.model_for("default")
        try:
            payload = response.json()
        except Exception:
            self._warning("ollama_model", "Ollama model list could not be parsed", "Run `ollama list` on the Ollama host.")
            return
        names = {str(item.get("name", "")) for item in payload.get("models", []) if isinstance(item, dict)}
        if model in names:
            self._passed("ollama_model", f"Ollama model is available: {model}")
        else:
            self._warning(
                "ollama_model",
                f"Ollama model is not listed: {model}",
                f"Pull the model on the Ollama host, for example: ollama pull {model}",
            )

    def _check_required_commands(self) -> None:
        assert self.config is not None
        policy = CommandPolicy(allowed_commands=self.config.allowed_commands, forbidden_commands=self.config.forbidden_commands)
        invalid = []
        for command in self.config.required_test_commands:
            try:
                policy.parse(command)
            except CommandPolicyError as exc:
                invalid.append(f"{command} ({exc})")
        if invalid:
            self._failed(
                "required_test_commands",
                "required_test_commands are not allowed: " + ", ".join(invalid),
                "Add safe commands to allowed_commands or remove them from required_test_commands.",
            )
        elif self.config.required_test_commands:
            self._passed("required_test_commands", "all required_test_commands are allowed by CommandPolicy")
        else:
            self._skipped("required_test_commands", "no required_test_commands configured")

    def _check_docker(self) -> None:
        assert self.config is not None
        if not self.config.docker_build_enabled and not self.config.docker_compose_enabled:
            self._skipped("docker", "Docker checks are disabled")
            return
        if not self.which("docker"):
            self._failed("docker", "Docker is enabled but docker is not installed", "Install Docker or set docker_build_enabled/docker_compose_enabled to false.")
            return
        self._passed("docker", "docker executable is installed")
        if self.config.docker_compose_enabled:
            result = self.run_command(["docker", "compose", "version"], cwd=Path("."), timeout_seconds=30)
            if result.ok:
                self._passed("docker_compose", "docker compose is available")
            else:
                self._failed("docker_compose", "docker compose is enabled but unavailable", "Install Docker Compose v2 or disable docker_compose_enabled.")

    def _check_repo_policy(self) -> None:
        assert self.config is not None
        policy_path = self.config.target_repo_path / self.config.repo_policy_file
        if policy_path.exists():
            self._passed("repo_policy", f"repo policy found: {self.config.repo_policy_file}")
        elif self.config.require_repo_policy_for_write:
            self._failed(
                "repo_policy",
                f"repo policy is required but missing: {self.config.repo_policy_file}",
                "Add .agentlab.yaml to the target repository or set require_repo_policy_for_write false for bootstrap/dry-run.",
            )
        else:
            self._warning("repo_policy", f"repo policy not found yet: {self.config.repo_policy_file}", "This is acceptable for bootstrap and dry-run.")

    def _check_dangerous_flags(self) -> None:
        assert self.config is not None
        enabled = [
            name
            for name in ("auto_merge_enabled", "direct_main_push_enabled", "push_agent_branches_enabled")
            if getattr(self.config, name)
        ]
        if enabled:
            self._warning(
                "dangerous_flags",
                "dangerous flags enabled: " + ", ".join(enabled),
                "Use only for explicitly approved MR/direct-main test modes.",
            )
        else:
            self._passed("dangerous_flags", "auto_merge, direct_main_push, and push_agent_branches are disabled")

    def _check_auto_approve(self) -> None:
        assert self.config is not None
        policy = self.config.auto_approve
        if not policy.enabled:
            self._passed("auto_approve", "auto_approve is disabled")
            return
        if self.config.direct_main_push_enabled or self.config.auto_merge_enabled:
            self._failed(
                "auto_approve",
                "auto_approve is enabled together with direct_main_push or auto_merge",
                "Disable direct_main_push_enabled and auto_merge_enabled for autonomous MR-flow.",
            )
            return
        broad = {"*", "**", "**/*"}
        if any(pattern in broad for pattern in policy.allowed_paths):
            self._warning(
                "auto_approve",
                "auto_approve is enabled with broad allowed_paths",
                "Narrow auto_approve.allowed_paths to documentation or test paths.",
            )
            return
        self._passed("auto_approve", "auto_approve is enabled with constrained paths")

    def _check_schedule(self) -> None:
        assert self.config is not None
        schedule = self.config.schedule
        if not schedule.enabled:
            self._passed("schedule", "schedule is disabled")
            return
        if not _is_numeric_project_id(self.config.project_id):
            self._warning(
                "scheduler_project_id",
                f"configured project_id is not numeric: {self.config.project_id}",
                'Numeric GitLab project ID is recommended for Scheduler/GitLab API reliability. Example: project_id: "5".',
            )
        self._check_scheduler_gitlab_api(enabled=True)
        failed = False
        if schedule.action.enabled and self.config.direct_main_push_enabled:
            self._failed("schedule", "scheduler action cannot run with direct_main_push_enabled", "Use MR-flow branch pushes for scheduler action.")
            failed = True
        if schedule.action.enabled and self.config.auto_merge_enabled:
            self._failed("schedule", "scheduler action cannot run with auto_merge_enabled", "Keep scheduler-created MRs reviewable; disable auto_merge_enabled.")
            failed = True
        if schedule.action.enabled and not self.config.auto_approve.enabled:
            self._failed("schedule", "scheduler action is enabled but auto_approve is disabled", "Enable auto_approve or disable schedule.action.")
            failed = True
        if schedule.action.enabled and not self.config.push_agent_branches_enabled:
            self._failed("schedule", "scheduler action is enabled but push_agent_branches_enabled is false", "Use mr-flow mode for scheduler action.")
            failed = True
        if schedule.action.enabled and schedule.limits.min_hours_between_action_runs < 2:
            self._warning("schedule", "scheduler action cooldown is below 2 hours", "Use a conservative action cooldown to avoid MR spam.")
            return
        if failed:
            return
        self._passed(
            "schedule",
            f"schedule enabled: watch={schedule.watch.cron}, plan={schedule.plan.cron}, action={schedule.action.cron}",
        )

    def _check_scheduler_gitlab_api(self, *, enabled: bool) -> None:
        assert self.config is not None
        if not self.environ.get(self.config.gitlab_token_env):
            self._skipped("scheduler_gitlab", "Scheduler GitLab API check skipped because token is missing")
            return
        try:
            gitlab = self.gitlab_tool_factory(self.config)
            head = gitlab.get_default_branch_head()
            open_mrs = len(gitlab.list_open_agent_mrs())
        except Exception as exc:
            message = (
                f"Scheduler GitLab API check failed for configured project_id={self.config.project_id}: {exc}"
            )
            remediation = (
                'Use the same project identifier scheduler-watch uses. Numeric GitLab project ID is recommended; '
                'example: project_id: "5". Check token scopes and project visibility.'
            )
            if enabled and (self.config.schedule.watch.enabled or self.config.schedule.action.enabled):
                self._failed("scheduler_gitlab", message, remediation)
            else:
                self._warning("scheduler_gitlab", message, remediation)
            return
        self._passed("scheduler_gitlab", f"Scheduler GitLab API works: default_branch_head={head}, open_agent_mrs={open_mrs}")

    def _passed(self, name: str, message: str) -> None:
        self.checks.append(PreflightCheck(name=name, status="passed", message=message))

    def _warning(self, name: str, message: str, remediation: str | None = None) -> None:
        self.checks.append(PreflightCheck(name=name, status="warning", message=message, remediation=remediation))

    def _failed(self, name: str, message: str, remediation: str | None = None) -> None:
        self.checks.append(PreflightCheck(name=name, status="failed", message=message, remediation=remediation))

    def _skipped(self, name: str, message: str) -> None:
        self.checks.append(PreflightCheck(name=name, status="skipped", message=message))


def run_doctor(config_path: str | Path) -> dict[str, Any]:
    return Doctor(config_path).run()


def format_doctor(report: dict[str, Any]) -> str:
    labels = {"passed": "PASS", "warning": "WARN", "failed": "FAIL", "skipped": "SKIP"}
    lines = [f"AgentLab doctor: {report['status']}"]
    for check in report["checks"]:
        label = labels.get(check["status"], check["status"].upper())
        lines.append(f"{label}: {check['message']}")
        remediation = check.get("remediation")
        if remediation:
            lines.append("Fix:")
            lines.extend(f"  {line}" if line else "" for line in str(remediation).splitlines())
    return "\n".join(lines)


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)


def _is_numeric_project_id(project_id: int | str | None) -> bool:
    if isinstance(project_id, int):
        return True
    return str(project_id or "").isdigit()
