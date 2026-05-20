from __future__ import annotations

import re

from agentlab.branching import agent_branch_name


def test_agent_branch_name_contains_task_and_run_id_short() -> None:
    branch = agent_branch_name("document-privileged-container-boundaries", "cb9b06c1ab70433ea9bfce1602691d3b")

    assert branch == "agent/document-privileged-container-boundaries-cb9b06c1"


def test_agent_branch_name_is_git_ref_safe() -> None:
    branch = agent_branch_name("Document Privileged/Container..Boundaries", "RUN ID ++ 1234")

    assert branch.startswith("agent/")
    assert " " not in branch
    assert ".." not in branch
    assert re.fullmatch(r"agent/[a-z0-9-]+", branch)
    assert "--" not in branch


def test_agent_branch_name_differs_for_repeated_task_runs() -> None:
    first = agent_branch_name("same-task", "aaaaaaaa11111111")
    second = agent_branch_name("same-task", "bbbbbbbb22222222")

    assert first != second
    assert first.startswith("agent/same-task-")
    assert second.startswith("agent/same-task-")
