from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlparse


MODES = {"safe-dry-run", "mr-flow", "auto-merge-test", "direct-main-test"}
DANGEROUS_MODES = {"auto-merge-test", "direct-main-test"}
TOKEN_PLACEHOLDER = "glpat-replace-me"


def derive_project_path_from_repo_url(repo_url: str) -> str | None:
    value = repo_url.strip()
    if not value:
        return None
    if "://" not in value and ":" in value and "@" in value.split(":", 1)[0]:
        path = value.split(":", 1)[1]
    else:
        path = urlparse(value).path
    path = unquote(path).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path or None


def project_identifier(*, project: str | None, project_id: str | None, target_repo_url: str) -> str:
    selected = project_id or project or derive_project_path_from_repo_url(target_repo_url)
    if not selected:
        raise ValueError("--project, --project-id, or a derivable --target-repo-url is required")
    return selected.strip()


def validate_mode(mode: str, *, allow_dangerous_mode: bool) -> str:
    if mode not in MODES:
        raise ValueError(f"unsupported mode: {mode}")
    if mode in DANGEROUS_MODES and not allow_dangerous_mode:
        raise ValueError(f"{mode} requires --allow-dangerous-mode")
    return mode


def mode_flags(mode: str) -> dict[str, object]:
    flags: dict[str, object] = {
        "auto_merge_enabled": False,
        "direct_main_push_enabled": False,
        "push_agent_branches_enabled": False,
    }
    if mode == "mr-flow":
        flags["push_agent_branches_enabled"] = True
    elif mode == "auto-merge-test":
        flags["push_agent_branches_enabled"] = True
        flags["auto_merge_enabled"] = True
    elif mode == "direct-main-test":
        flags["direct_main_push_enabled"] = True
        flags["max_risk_score_for_direct_main_push"] = 10
    return flags


def yaml_string(value: str) -> str:
    return json.dumps(value)


def yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def render_agentlab_config(
    *,
    gitlab_url: str,
    project_id: str,
    target_repo_url: str,
    target_repo_ref: str,
    ollama_url: str,
    model: str,
    workspace_root: str,
    mode: str,
    runtime: str,
    schedule_enabled: bool = False,
    schedule_watch_cron: str = "*/30 * * * *",
    schedule_plan_cron: str = "0 7,19 * * *",
    schedule_action_cron: str = "30 2 * * *",
    schedule_review_comments_enabled: bool = False,
    schedule_review_comments_cron: str = "*/15 * * * *",
    k8s_resource_profile_preset: str = "default",
    cargo_build_jobs: str = "1",
) -> str:
    flags = mode_flags(mode)
    require_repo_policy = "false"
    lines = [
        f"gitlab_url: {yaml_string(gitlab_url)}",
        f"project_id: {yaml_string(project_id)}",
        f"default_branch: {yaml_string(target_repo_ref)}",
        'gitlab_token_env: "GITLAB_TOKEN"',
        "",
        f"target_repo_url: {yaml_string(target_repo_url)}",
        'target_repo_path: "/workspace/repo"',
        f"target_repo_ref: {yaml_string(target_repo_ref)}",
        "clone_target_repo: true",
        f"workspace_root: {yaml_string(workspace_root)}",
        "",
        "ollama:",
        f"  base_url: {yaml_string(ollama_url)}",
        "  models:",
        f"    default: {yaml_string(model)}",
        f"    planner: {yaml_string(model)}",
        f"    implementer: {yaml_string(model)}",
        f"    review_quality: {yaml_string(model)}",
        f"    review_security: {yaml_string(model)}",
        "",
        f"auto_merge_enabled: {yaml_bool(bool(flags['auto_merge_enabled']))}",
        f"direct_main_push_enabled: {yaml_bool(bool(flags['direct_main_push_enabled']))}",
        f"push_agent_branches_enabled: {yaml_bool(bool(flags['push_agent_branches_enabled']))}",
        "",
        "schedule:",
        f"  enabled: {yaml_bool(schedule_enabled)}",
        '  timezone: "Europe/Berlin"',
        "  watch:",
        "    enabled: true",
        f"    cron: {yaml_string(schedule_watch_cron)}",
        "  plan:",
        "    enabled: true",
        f"    cron: {yaml_string(schedule_plan_cron)}",
        "  action:",
        "    enabled: true",
        f"    cron: {yaml_string(schedule_action_cron)}",
        "  review_comments:",
        f"    enabled: {yaml_bool(schedule_review_comments_enabled)}",
        f"    cron: {yaml_string(schedule_review_comments_cron)}",
        "    process_history: false",
        "    max_comments_per_run: 1",
        "    cooldown_minutes: 10",
        "    allowed_commands:",
        "      - revise",
        "      - fix",
        "      - status",
        "      - merge-status",
        "      - explain",
        "      - stop",
        "      - resume",
        "    allowed_authors: []",
        "    require_author_role:",
        "      - owner",
        "      - maintainer",
        "",
        "docker_build_enabled: false",
        "docker_compose_enabled: false",
        "k8s_resource_profile:",
        f"  preset: {yaml_string(k8s_resource_profile_preset)}",
        "functional_test_env:",
        f"  CARGO_BUILD_JOBS: {yaml_string(cargo_build_jobs)}",
        f"require_repo_policy_for_write: {require_repo_policy}",
        "",
        "required_test_commands: []",
    ]
    if runtime == "kubernetes":
        lines.insert(-1, "")
    if "max_risk_score_for_direct_main_push" in flags:
        lines.extend(["", "max_risk_score_for_direct_main_push: 10"])
    return "\n".join(lines)


def gitlab_host(gitlab_url: str) -> str:
    parsed = urlparse(gitlab_url)
    return parsed.hostname or "gitlab.local"


def ensure_no_real_token(content: str) -> None:
    forbidden = ("glpat-", "gloas-", "glrt-", "PRIVATE-TOKEN")
    for marker in forbidden:
        if marker in content and TOKEN_PLACEHOLDER not in content:
            raise ValueError("generated content appears to contain a GitLab token")
