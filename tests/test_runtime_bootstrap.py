from __future__ import annotations

import pytest
import yaml

from scripts.bootstrap_common import TOKEN_PLACEHOLDER, derive_project_path_from_repo_url
from scripts.bootstrap_docker import generate_docker
from scripts.bootstrap_komodo import generate_komodo
from scripts.bootstrap_k8s import generate_k8s


def read_all(output_dir):
    return "\n".join(path.read_text(encoding="utf-8") for path in output_dir.rglob("*") if path.is_file())


def config_from_configmap(output_dir):
    configmap = yaml.safe_load((output_dir / "configmap.yaml").read_text(encoding="utf-8"))
    return yaml.safe_load(configmap["data"]["config.yaml"])


def env_from_job(output_dir, job_name="job-dry-run.yaml"):
    job = yaml.safe_load((output_dir / job_name).read_text(encoding="utf-8"))
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item for item in container["env"]}


def test_kubernetes_bootstrap_generates_expected_files_without_secrets(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    expected = {
        "namespace.yaml",
        "serviceaccount.yaml",
        "pvc.yaml",
        "configmap.yaml",
        "secret.example.yaml",
        "job-doctor.yaml",
        "job-dry-run.yaml",
        "job-index.yaml",
        "job-steward.yaml",
        "job-plan.yaml",
        "job-run-task.yaml",
        "job-full-flow.yaml",
        "kustomization.yaml",
        "README.generated.md",
    }
    assert {path.name for path in out.iterdir()} == expected
    content = read_all(out)
    assert "real-token" not in content
    assert "/var/run/docker.sock" not in content
    assert "privileged: true" not in content
    assert "glpat-replace-me" in (out / "secret.example.yaml").read_text(encoding="utf-8")
    assert not list(out.glob("cronjob-*.yaml"))


def test_kubernetes_configmap_safe_defaults_and_connection_values(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    raw_configmap = (out / "configmap.yaml").read_text(encoding="utf-8")
    config = config_from_configmap(out)

    assert "registry.local/agentlab:0.1.0" in raw_configmap
    assert config["gitlab_url"] == "https://gitlab.local"
    assert config["project_id"] == "group/project"
    assert config["target_repo_url"] == "https://gitlab.local/group/project.git"
    assert config["ollama"]["base_url"] == "http://ollama.local:11434"
    assert config["auto_merge_enabled"] is False
    assert config["direct_main_push_enabled"] is False
    assert config["push_agent_branches_enabled"] is False


def test_kubernetes_mr_flow_enables_only_branch_push(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
        mode="mr-flow",
    )

    config = config_from_configmap(out)
    assert config["push_agent_branches_enabled"] is True
    assert config["auto_merge_enabled"] is False
    assert config["direct_main_push_enabled"] is False


@pytest.mark.parametrize("mode", ["auto-merge-test", "direct-main-test"])
def test_kubernetes_dangerous_modes_require_explicit_allow(tmp_path, mode):
    with pytest.raises(ValueError, match="allow-dangerous-mode"):
        generate_k8s(
            namespace="agentlab",
            image="registry.local/agentlab:0.1.0",
            gitlab_url="https://gitlab.local",
            target_repo_url="https://gitlab.local/group/project.git",
            ollama_url="http://ollama.local:11434",
            output_dir=tmp_path,
            mode=mode,
        )


def test_kubernetes_jobs_use_security_context_and_secret_env(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )
    doctor_job = yaml.safe_load((out / "job-doctor.yaml").read_text(encoding="utf-8"))
    dry_run_job = yaml.safe_load((out / "job-dry-run.yaml").read_text(encoding="utf-8"))

    pod_spec = doctor_job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["command"] == ["agentlab"]
    assert container["args"] == ["doctor", "--config", "/etc/agentlab/config.yaml"]
    assert dry_run_job["spec"]["template"]["spec"]["containers"][0]["args"][0] == "dry-run"
    assert container["env"][0]["valueFrom"]["secretKeyRef"]["key"] == "GITLAB_TOKEN"


def test_kubernetes_jobs_configure_noninteractive_gitlab_https_auth(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    job = yaml.safe_load((out / "job-dry-run.yaml").read_text(encoding="utf-8"))
    pod_spec = job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = {item["name"]: item for item in container["env"]}

    assert env["GIT_TERMINAL_PROMPT"]["value"] == "0"
    assert env["GIT_CONFIG_COUNT"]["value"] == "3"
    assert env["GIT_CONFIG_KEY_0"]["value"] == "credential.helper"
    assert "$GITLAB_TOKEN" in env["GIT_CONFIG_VALUE_0"]["value"]
    assert "glpat-" not in env["GIT_CONFIG_VALUE_0"]["value"]
    assert TOKEN_PLACEHOLDER not in env["GIT_CONFIG_VALUE_0"]["value"]
    assert env["GIT_CONFIG_KEY_1"]["value"] == "user.name"
    assert env["GIT_CONFIG_VALUE_1"]["value"] == "AgentLab Bot"
    assert env["GIT_CONFIG_KEY_2"]["value"] == "user.email"
    assert env["GIT_CONFIG_VALUE_2"]["value"] == "agentlab-bot@example.local"

    git_netrc = next(volume for volume in pod_spec["volumes"] if volume["name"] == "git-netrc")
    default_mode = git_netrc["secret"]["defaultMode"]
    assert default_mode != 0o600
    assert default_mode & 0o040
    assert pod_spec["securityContext"]["fsGroup"] == 10001


def test_kubernetes_jobs_use_collision_free_git_config_indices(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    env = env_from_job(out)
    count = int(env["GIT_CONFIG_COUNT"]["value"])
    keys = [env[f"GIT_CONFIG_KEY_{index}"]["value"] for index in range(count)]
    values = [env[f"GIT_CONFIG_VALUE_{index}"]["value"] for index in range(count)]

    assert keys == ["credential.helper", "user.name", "user.email"]
    assert len(set(keys)) == count
    assert any("$GITLAB_TOKEN" in value for value in values)


def test_kubernetes_git_author_options_override_defaults(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
        git_author_name="Custom Bot",
        git_author_email="custom-bot@example.local",
    )

    env = env_from_job(out, "job-run-task.yaml")
    assert env["GIT_CONFIG_VALUE_1"]["value"] == "Custom Bot"
    assert env["GIT_CONFIG_VALUE_2"]["value"] == "custom-bot@example.local"
    assert env["GIT_CONFIG_VALUE_0"]["value"] == "!f() { echo username=oauth2; echo password=$GITLAB_TOKEN; }; f"


def test_kubernetes_kustomization_references_base_resources(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    kustomization = yaml.safe_load((out / "kustomization.yaml").read_text(encoding="utf-8"))
    assert set(kustomization["resources"]) == {"namespace.yaml", "serviceaccount.yaml", "pvc.yaml", "configmap.yaml"}


def test_kubernetes_bootstrap_generates_scheduler_cronjobs_when_enabled(tmp_path):
    out = generate_k8s(
        namespace="agentlab",
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
        schedule_enabled=True,
        schedule_watch_cron="*/15 * * * *",
        schedule_plan_cron="0 8 * * *",
        schedule_action_cron="30 2 * * *",
    )

    for name, cron, command in (
        ("watch", "*/15 * * * *", "scheduler-watch"),
        ("plan", "0 8 * * *", "scheduler-plan"),
        ("action", "30 2 * * *", "scheduler-action"),
    ):
        cronjob = yaml.safe_load((out / f"cronjob-{name}.yaml").read_text(encoding="utf-8"))
        assert cronjob["kind"] == "CronJob"
        assert cronjob["spec"]["schedule"] == cron
        assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"
        assert cronjob["spec"]["successfulJobsHistoryLimit"] == 3
        assert cronjob["spec"]["failedJobsHistoryLimit"] == 5
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        assert container["args"][0] == command
        volume_names = {volume["name"] for volume in cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]}
        assert {"config", "git-netrc", "runs"}.issubset(volume_names)

    config = config_from_configmap(out)
    assert config["schedule"]["enabled"] is True
    assert config["schedule"]["watch"]["cron"] == "*/15 * * * *"


def test_docker_bootstrap_generates_compose_config_and_readme(tmp_path):
    out = generate_docker(
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        project="group/project",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    assert (out / "compose.yaml").exists()
    assert (out / "config.yaml").exists()
    assert (out / ".env.agentlab.example").exists()
    assert (out / "README.generated.md").exists()
    content = read_all(out)
    assert "real-token" not in content
    assert "/var/run/docker.sock" not in (out / "compose.yaml").read_text(encoding="utf-8")
    assert "privileged: true" not in content


def test_docker_compose_services_and_security_defaults(tmp_path):
    out = generate_docker(
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        project="group/project",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path,
    )

    compose = yaml.safe_load((out / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {
        "agentlab-doctor",
        "agentlab-dry-run",
        "agentlab-index",
        "agentlab-steward",
        "agentlab-plan",
        "agentlab-full-flow",
    }
    doctor = compose["services"]["agentlab-doctor"]
    assert doctor["command"] == "agentlab doctor --config /etc/agentlab/config.yaml"
    assert doctor["cap_drop"] == ["ALL"]
    assert doctor["security_opt"] == ["no-new-privileges:true"]
    assert doctor["read_only"] is True
    assert "./config.yaml:/etc/agentlab/config.yaml:ro" in doctor["volumes"]


def test_docker_modes_and_dangerous_guards(tmp_path):
    out = generate_docker(
        image="registry.local/agentlab:0.1.0",
        gitlab_url="https://gitlab.local",
        project="group/project",
        target_repo_url="https://gitlab.local/group/project.git",
        ollama_url="http://ollama.local:11434",
        output_dir=tmp_path / "mr",
        mode="mr-flow",
    )
    config = yaml.safe_load((out / "config.yaml").read_text(encoding="utf-8"))
    assert config["push_agent_branches_enabled"] is True
    assert config["auto_merge_enabled"] is False
    assert config["direct_main_push_enabled"] is False

    with pytest.raises(ValueError, match="allow-dangerous-mode"):
        generate_docker(
            image="registry.local/agentlab:0.1.0",
            gitlab_url="https://gitlab.local",
            project="group/project",
            target_repo_url="https://gitlab.local/group/project.git",
            ollama_url="http://ollama.local:11434",
            output_dir=tmp_path / "danger",
            mode="direct-main-test",
        )


def test_project_path_is_derived_from_repo_urls():
    assert derive_project_path_from_repo_url("https://gitlab.local/group/project.git") == "group/project"
    assert derive_project_path_from_repo_url("git@gitlab.local:group/sub/project.git") == "group/sub/project"


def test_komodo_bootstrap_generates_optional_docs_without_secrets(tmp_path):
    out = generate_komodo(namespace="agentlab", output_dir=tmp_path)

    assert {path.name for path in out.iterdir()} == {"README.md", "job-triggers.md", "agentlab-komodo.example.yaml"}
    content = read_all(out)
    assert "Komodo integration is optional" in content
    assert "agentlab-secrets" in content
    assert "glpat-" not in content
