from __future__ import annotations

import pytest

from agentlab.tools.gitlab_tool import GitLabTool


class FakeMR:
    def __init__(
        self,
        *,
        draft: bool = False,
        has_conflicts: bool = False,
        state: str = "opened",
        detailed_merge_status: str | None = "mergeable",
        merge_status: str | None = "can_be_merged",
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
        self.merged = False

    def merge(self, *, squash: bool = True) -> None:
        self.merged = True


class FakeMRManager:
    def __init__(self, mr: FakeMR) -> None:
        self.mr = mr

    def get(self, mr_id: int) -> FakeMR:
        return self.mr


class FakeProject:
    def __init__(self, mr: FakeMR) -> None:
        self.mergerequests = FakeMRManager(mr)


def tool_for(mr: FakeMR) -> GitLabTool:
    tool = GitLabTool.__new__(GitLabTool)
    tool.project = FakeProject(mr)
    return tool


@pytest.mark.parametrize(
    "mr,error",
    [
        (FakeMR(draft=True), "draft"),
        (FakeMR(has_conflicts=True), "conflicts"),
        (FakeMR(state="closed"), "state"),
        (FakeMR(detailed_merge_status="not_open"), "not mergeable"),
        (FakeMR(detailed_merge_status="checking"), "not mergeable"),
        (FakeMR(detailed_merge_status=None, merge_status="unchecked"), "not mergeable"),
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
