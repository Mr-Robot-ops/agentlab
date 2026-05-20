from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .bootstrap_common import (
        TOKEN_PLACEHOLDER,
        project_identifier,
        render_agentlab_config,
        validate_mode,
        write_file,
    )
except ImportError:  # pragma: no cover
    from bootstrap_common import (  # type: ignore
        TOKEN_PLACEHOLDER,
        project_identifier,
        render_agentlab_config,
        validate_mode,
        write_file,
    )


SERVICES = {
    "agentlab-doctor": "agentlab doctor --config /etc/agentlab/config.yaml",
    "agentlab-dry-run": "agentlab dry-run --config /etc/agentlab/config.yaml",
    "agentlab-index": "agentlab index --config /etc/agentlab/config.yaml",
    "agentlab-steward": "agentlab steward --config /etc/agentlab/config.yaml",
    "agentlab-plan": "agentlab plan --config /etc/agentlab/config.yaml",
    "agentlab-full-flow": "agentlab full-flow --config /etc/agentlab/config.yaml",
}


def generate_docker(
    *,
    image: str,
    gitlab_url: str,
    target_repo_url: str,
    ollama_url: str,
    project: str | None = None,
    project_id: str | None = None,
    target_repo_ref: str = "main",
    model: str = "qwen3.6:35b",
    mode: str = "safe-dry-run",
    output_dir: str | Path = "deploy/docker/generated",
    workspace_dir: str = "./workspace",
    runs_dir: str = "./runs",
    allow_dangerous_mode: bool = False,
) -> Path:
    mode = validate_mode(mode, allow_dangerous_mode=allow_dangerous_mode)
    project_value = project_identifier(project=project, project_id=project_id, target_repo_url=target_repo_url)
    out = Path(output_dir)
    config_yaml = render_agentlab_config(
        gitlab_url=gitlab_url,
        project_id=project_value,
        target_repo_url=target_repo_url,
        target_repo_ref=target_repo_ref,
        ollama_url=ollama_url,
        model=model,
        workspace_root="/var/lib/agentlab/runs",
        mode=mode,
        runtime="docker",
    )
    write_file(out / "compose.yaml", render_compose(image=image, workspace_dir=workspace_dir, runs_dir=runs_dir))
    write_file(out / "config.yaml", config_yaml)
    write_file(out / ".env.agentlab.example", f"GITLAB_TOKEN={TOKEN_PLACEHOLDER}")
    write_file(out / "README.generated.md", render_readme())
    return out


def render_compose(*, image: str, workspace_dir: str, runs_dir: str) -> str:
    blocks = []
    for service, command in SERVICES.items():
        blocks.append(
            f"""  {service}:
    image: "{image}"
    command: {command}
    env_file:
      - .env.agentlab
    working_dir: /workspace
    read_only: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    tmpfs:
      - /tmp
    volumes:
      - ./config.yaml:/etc/agentlab/config.yaml:ro
      - {workspace_dir}:/workspace
      - {runs_dir}:/var/lib/agentlab/runs"""
        )
    return "services:\n" + "\n".join(blocks) + "\n"


def render_readme() -> str:
    return """# Generated AgentLab Docker Compose Runtime

Docker Compose is intended for fast local tests or a small single-server runtime. Kubernetes remains the recommended production runtime.

```bash
cd deploy/docker/generated
cp .env.agentlab.example .env.agentlab
# edit .env.agentlab and set GITLAB_TOKEN
docker compose run --rm agentlab-doctor
docker compose run --rm agentlab-dry-run
docker compose run --rm agentlab-index
docker compose run --rm agentlab-steward
```

For MR flow, regenerate with:

```bash
python ../../../scripts/bootstrap_docker.py --mode mr-flow <same connection options>
docker compose run --rm agentlab-full-flow
```

Token notes:

- Do not commit `.env.agentlab`.
- Add `.env.agentlab` to your local `.gitignore` if needed.
- `compose.yaml` contains no tokens.
- The token for MR flow needs `api`, `read_repository`, and `write_repository`.

Docker-in-Docker is not the default. This generated runtime does not mount `/var/run/docker.sock`. For build checks, prefer GitLab CI, an external runner, Kaniko, or rootless BuildKit.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate AgentLab Docker Compose runtime files.")
    parser.add_argument("--image", required=True)
    parser.add_argument("--gitlab-url", required=True)
    parser.add_argument("--project")
    parser.add_argument("--project-id")
    parser.add_argument("--target-repo-url", required=True)
    parser.add_argument("--target-repo-ref", default="main")
    parser.add_argument("--ollama-url", required=True)
    parser.add_argument("--model", default="qwen3.6:35b")
    parser.add_argument("--mode", default="safe-dry-run")
    parser.add_argument("--output-dir", default="deploy/docker/generated")
    parser.add_argument("--workspace-dir", default="./workspace")
    parser.add_argument("--runs-dir", default="./runs")
    parser.add_argument("--allow-dangerous-mode", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        out = generate_docker(
            image=args.image,
            gitlab_url=args.gitlab_url,
            project=args.project,
            project_id=args.project_id,
            target_repo_url=args.target_repo_url,
            target_repo_ref=args.target_repo_ref,
            ollama_url=args.ollama_url,
            model=args.model,
            mode=args.mode,
            output_dir=args.output_dir,
            workspace_dir=args.workspace_dir,
            runs_dir=args.runs_dir,
            allow_dangerous_mode=args.allow_dangerous_mode,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Generated Docker Compose runtime in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
