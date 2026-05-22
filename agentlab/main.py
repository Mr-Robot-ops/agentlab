from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from agentlab.config import load_config
from agentlab.doctor import format_doctor, report_json, run_doctor
from agentlab.k8s_cli import k8s_app
from agentlab.models import AgentTask
from agentlab.orchestrator import Orchestrator
from agentlab.preflight import PreflightChecker
from agentlab.release_cli import release_app
from agentlab.scheduler import Scheduler, reset_scheduler_state, scheduler_status
from agentlab.status import TERMINAL_STATES, format_status, list_run_statuses, read_run_status

app = typer.Typer(help="AgentLab GitLab agent orchestration CLI.")
app.add_typer(k8s_app, name="k8s")
app.add_typer(release_app, name="release")


def _json_echo(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


@app.command()
def index(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    repo_index, architecture = orchestrator.index_repository()
    _json_echo(
        {
            "run_id": orchestrator.run_id,
            "repo_index": repo_index.model_dump(mode="json"),
            "architecture_summary": architecture.model_dump(mode="json"),
        }
    )


@app.command()
def steward(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    result = orchestrator.steward()
    _json_echo({"run_id": orchestrator.run_id, "steward": result.model_dump(mode="json")})


@app.command("supply-chain")
def supply_chain(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    result = orchestrator.supply_chain()
    _json_echo({"run_id": orchestrator.run_id, "supply_chain": result.model_dump(mode="json")})


@app.command()
def provenance(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    result = orchestrator.provenance()
    _json_echo({"run_id": orchestrator.run_id, "provenance": result.model_dump(mode="json")})


@app.command()
def plan(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    result = orchestrator.plan()
    _json_echo({"run_id": orchestrator.run_id, "plan": result.model_dump(mode="json")})


@app.command("run-task")
def run_task(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    task: Path = typer.Option(..., "--task", exists=True, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    cfg = load_config(config)
    agent_task = AgentTask.model_validate_json(task.read_text(encoding="utf-8"))
    orchestrator = Orchestrator(cfg, dry_run=dry_run)
    result = orchestrator.run_task(agent_task)
    _json_echo({"run_id": orchestrator.run_id, "implementation": result.model_dump(mode="json")})


@app.command("full-flow")
def full_flow(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    _json_echo(Orchestrator(cfg).full_flow())


@app.command("scheduler-watch")
def scheduler_watch(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    _json_echo(Scheduler(cfg).watch())


@app.command("scheduler-plan")
def scheduler_plan(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    _json_echo(Scheduler(cfg).plan())


@app.command("scheduler-action")
def scheduler_action(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    task_id: str | None = typer.Option(None, "--task-id", help="Run a specific approved scheduler task by ID."),
    prefer_task_type: list[str] | None = typer.Option(None, "--prefer-task-type", help="Prefer approved tasks of this type. Repeat to set priority order."),
    prefer_task_id: list[str] | None = typer.Option(None, "--prefer-task-id", help="Prefer this approved task ID. Repeat to set priority order."),
) -> None:
    cfg = load_config(config)
    result = Scheduler(cfg).action(
        task_id=task_id,
        prefer_task_types=prefer_task_type,
        prefer_task_ids=prefer_task_id,
    )
    _json_echo(result)
    if task_id is not None and result.get("status") == "failed":
        raise typer.Exit(code=1)


@app.command("scheduler-review-comments")
def scheduler_review_comments(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    process_history: bool = typer.Option(False, "--process-history", help="Process historical /agent comments instead of initializing high-water marks."),
) -> None:
    cfg = load_config(config)
    if process_history:
        review_comments = cfg.schedule.review_comments.model_copy(update={"process_history": True})
        schedule = cfg.schedule.model_copy(update={"review_comments": review_comments})
        cfg = cfg.model_copy(update={"schedule": schedule})
    _json_echo(Scheduler(cfg).review_comments())


@app.command("scheduler-reset-state")
def scheduler_reset_state(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    _json_echo(reset_scheduler_state(cfg))


@app.command("scheduler-status")
def scheduler_state_status(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    _json_echo(scheduler_status(cfg))


@app.command("review-mr")
def review_mr(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    mr_id: int = typer.Option(..., "--mr-id"),
) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg)
    result = orchestrator.review_existing_mr(mr_id)
    _json_echo({"run_id": orchestrator.run_id, **result})


@app.command()
def recover(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    ref: str | None = typer.Option(None, "--ref"),
    commit_sha: str | None = typer.Option(None, "--commit-sha"),
) -> None:
    cfg = load_config(config)
    _json_echo(Orchestrator(cfg).recover(ref=ref, commit_sha=commit_sha))


@app.command("dry-run")
def dry_run(config: Path = typer.Option(..., "--config", exists=True, readable=True)) -> None:
    cfg = load_config(config)
    orchestrator = Orchestrator(cfg, dry_run=True)
    _json_echo({"run_id": orchestrator.run_id, "plan": orchestrator.plan().model_dump(mode="json")})


@app.command()
def doctor(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    report = run_doctor(config)
    typer.echo(report_json(report) if json_output else format_doctor(report))
    exit_code = int(report["exit_code"])
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command()
def preflight(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    mode: str = typer.Option("full-flow", "--mode"),
) -> None:
    cfg = load_config(config)
    report = PreflightChecker(cfg, mode=mode).run()
    _json_echo(report.model_dump(mode="json"))
    if not report.passed:
        raise typer.Exit(code=2)


@app.command()
def status(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    run_id: str | None = typer.Option(None, "--run-id"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    human: bool = typer.Option(False, "--human"),
) -> None:
    cfg = load_config(config)
    if run_id:
        snapshot = read_run_status(cfg, run_id)
        typer.echo(format_status(snapshot) if human else snapshot.model_dump_json(indent=2))
        return
    snapshots = list_run_statuses(cfg, limit=limit)
    payload = [snapshot.model_dump(mode="json") for snapshot in snapshots]
    _json_echo(payload)


@app.command()
def watch(
    config: Path = typer.Option(..., "--config", exists=True, readable=True),
    run_id: str = typer.Option(..., "--run-id"),
    interval: float = typer.Option(2.0, "--interval", min=0.5),
    stop_on_terminal: bool = typer.Option(True, "--stop-on-terminal/--follow"),
) -> None:
    cfg = load_config(config)
    last_rendered = ""
    while True:
        snapshot = read_run_status(cfg, run_id)
        rendered = format_status(snapshot)
        if rendered != last_rendered:
            typer.echo(rendered)
            typer.echo("")
            last_rendered = rendered
        if stop_on_terminal and snapshot.state in TERMINAL_STATES:
            return
        time.sleep(interval)


if __name__ == "__main__":
    app()
