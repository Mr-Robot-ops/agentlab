from __future__ import annotations

import re


def agent_branch_name(task_id: str, run_id: str, *, run_id_length: int = 8, max_length: int = 120) -> str:
    task_slug = _slug(task_id) or "task"
    run_slug = _slug(run_id.lower())[:run_id_length] or "run"
    suffix = f"-{run_slug}"
    max_task_length = max(1, max_length - len("agent/") - len(suffix))
    task_slug = task_slug[:max_task_length].strip("-") or "task"
    return f"agent/{task_slug}{suffix}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip())
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    return slug
