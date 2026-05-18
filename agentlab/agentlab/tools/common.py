from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence

from agentlab.audit import redact_secrets
from agentlab.models import CommandResult


class ToolError(RuntimeError):
    pass


def ensure_within(base: Path, candidate: Path) -> Path:
    resolved_base = base.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_base and resolved_base not in resolved_candidate.parents:
        raise ToolError(f"path escapes workspace: {candidate}")
    return resolved_candidate


def run_subprocess(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: int,
    input_text: str | None = None,
) -> CommandResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timed_out = True
    except FileNotFoundError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
        timed_out = False

    return CommandResult(
        command=" ".join(command),
        cwd=str(Path(cwd).resolve()),
        exit_code=exit_code,
        stdout=redact_secrets(stdout),
        stderr=redact_secrets(stderr),
        duration_seconds=time.monotonic() - start,
        timed_out=timed_out,
    )
