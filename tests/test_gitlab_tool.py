from __future__ import annotations

import pytest

from agentlab.tools.gitlab_tool import GitLabTool


class FakeMR:
    def __init__(
        self,
        *,
        draft: bool = False,
        has_conflicts: bool = False,
        state: str | None = "opened",
        detailed_merge_status: str | None = "mergeable",
        merge_status: str | None = "can_be_merged",
        work_in_progress: bool = False,
    ) -> None:
        self.id = 1
        self.iid = 7
        self.title = "MR"
        self.source_branch = "agent/t1"
        self.target_branch = "main"
        self.labels: list[str] = []
        self.draft = draft
        self.has_conflicts = has_conflicts
        self.state = state
        self.detailed_merge_status = detailed_merge_status
        self.merge_status = merge_status
        self.work_in_progress = work_in_progress
        self.merged = False

    def merge(self, *, squash: bool = True) -> None:
        self.merged = True


class FakeMRManager:
    def __init__(self, mr: FakeMR) -> None:
        self.mr = mr

    def get(self, mr_id: int) -> FakeMR:
        return self.mr

    def list(self, **kwargs):
        return [self.mr]


class FakeProject:
    def __init__(self, mr: FakeMR) -> None:
        self.mergerequests = FakeMRManager(mr)


class FakeBranchManager:
    def get(self, branch: str):
        class Branch:
            commit = {"id": "abc123"}

        return Branch()


def tool_for(mr: FakeMR) -> GitLabTool:
    tool = GitLabTool.__new__(GitLabTool)
    tool.project = FakeProject(mr)
    return tool


@pytest.mark.parametrize(
    "mr,error",
    [
        (FakeMR(draft=True), "draft"),
        (FakeMR(work_in_progress=True), "draft"),
        (FakeMR(has_conflicts=True), "conflicts"),
        (FakeMR(state="closed"), "state"),
        (FakeMR(state=None), "state"),
        (FakeMR(detailed_merge_status="not_open"), "not mergeable"),
        (FakeMR(detailed_merge_status="checking"), "not mergeable"),
        (FakeMR(detailed_merge_status="unknown"), "not mergeable"),
        (FakeMR(detailed_merge_status=None, merge_status="unchecked"), "not mergeable"),
        (FakeMR(detailed_merge_status=None, merge_status=None), "unknown"),
    ],
)
def test_merge_mr_guarded_blocks_unsafe_states(mr: FakeMR, error: str) -> None:
    with pytest.raises(RuntimeError, match=error):
        tool_for(mr).merge_mr_guarded(7)
    assert mr.merged is False


def test_merge_mr_guarded_allows_mergeable_detailed_status() -> None:
    mr = FakeMR(detailed_merge_status="mergeable")

    result = tool_for(mr).merge_mr_guarded(7)

    assert mr.merged is True
    assert result.iid == 7


def test_merge_mr_guarded_allows_can_be_merged_detailed_status() -> None:
    mr = FakeMR(detailed_merge_status="can_be_merged")

    result = tool_for(mr).merge_mr_guarded(7)

    assert mr.merged is True
    assert result.iid == 7


def test_merge_mr_guarded_allows_can_be_merged_when_detailed_missing() -> None:
    mr = FakeMR(detailed_merge_status=None, merge_status="can_be_merged")

    result = tool_for(mr).merge_mr_guarded(7)

    assert mr.merged is True
    assert result.iid == 7


def test_list_open_agent_mrs_filters_agent_source_branch() -> None:
    mr = FakeMR()
    tool = tool_for(mr)
    tool.config = type("Config", (), {"default_branch": "main"})()

    result = tool.list_open_agent_mrs()

    assert len(result) == 1
    assert result[0].source_branch == "agent/t1"


def test_get_default_branch_head_reads_branch_commit() -> None:
    mr = FakeMR()
    tool = tool_for(mr)
    tool.config = type("Config", (), {"default_branch": "main"})()
    tool.project.branches = FakeBranchManager()

    assert tool.get_default_branch_head() == "abc123"
