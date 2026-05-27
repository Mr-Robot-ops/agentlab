from __future__ import annotations

from typing import Any

from agentlab.agents.planner import PlanningAgent
from agentlab.config import AppConfig
from agentlab.models import AgentTask, RiskLevel, TaskPlan, TaskType
from agentlab.policies.auto_approval import AutoApprovalPolicy


class FakeFileTool:
    def __init__(self, files: list[str], contents: dict[str, str] | None = None) -> None:
        self.files = files
        self.contents = contents or {}

    def list_files(self) -> list[str]:
        return self.files

    def read_file(self, path: str) -> str:
        return self.contents.get(path, "")

    def search_text(self, pattern: str) -> list[str]:
        return []


class FakeOllama:
    def __init__(self, plan: TaskPlan) -> None:
        self.plan = plan

    def chat_json(self, **kwargs: Any) -> TaskPlan:
        return self.plan


def config(tmp_path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=tmp_path,
        workspace_root=tmp_path / "runs",
        supply_chain_enabled=False,
        provenance_enabled=False,
    )


def approval_config(tmp_path, *, allowed_paths: list[str]) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=tmp_path,
        workspace_root=tmp_path / "runs",
        supply_chain_enabled=False,
        provenance_enabled=False,
        auto_approve={
            "enabled": True,
            "allowed_task_types": ["docs", "tests"],
            "allowed_paths": allowed_paths,
            "blocked_paths": [".gitlab-ci.yml", "deploy/**", "Dockerfile", "compose.yaml", "**/.env"],
        },
    )


def rust_smoke_feedback(count: int = 3) -> list[dict[str, object]]:
    return [
        {
            "iid": 18 + index,
            "title": "Add Rust backend smoke test baseline",
            "source_branch": f"agent/rust-smoke-baseline-{index}",
            "changed_files": ["rust-backend/tests/smoke.rs"],
            "reason": "binary-only crate needs a public library seam",
        }
        for index in range(count)
    ]


def test_minimal_rust_smoke_test_task_prefers_test_only_files(tmp_path) -> None:
    bad_plan = TaskPlan(
        summary="Add Rust test baseline.",
        tasks=[
            AgentTask(
                id="rust-smoke-test-baseline",
                title="Add minimal Rust smoke test baseline",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                description="Add a minimal Rust smoke test.",
                acceptance_criteria=["A smoke test runs with cargo test."],
                affected_files=[
                    "rust-backend/Cargo.toml",
                    "rust-backend/src/error.rs",
                    "rust-backend/src/state.rs",
                ],
                forbidden_actions=["Do not change production behavior."],
                test_requirements=["cargo test"],
            )
        ],
    )
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/lib.rs", "rust-backend/src/error.rs", "rust-backend/src/state.rs"]),
        FakeOllama(bad_plan),
    )

    plan = agent.plan()
    task = plan.tasks[0]

    assert task.affected_files == ["rust-backend/tests/smoke.rs"]
    assert "rust-backend/src/error.rs" not in task.affected_files
    assert "rust-backend/src/state.rs" not in task.affected_files
    assert task.risk_level == RiskLevel.LOW
    assert task.risk_score == 3
    assert task.metadata["removed_production_files"] == ["rust-backend/src/error.rs", "rust-backend/src/state.rs"]


def test_rust_smoke_test_keeps_cargo_only_when_dev_dependencies_are_required(tmp_path) -> None:
    plan_with_dev_dep = TaskPlan(
        summary="Add Rust integration test dependency.",
        tasks=[
            AgentTask(
                id="rust-smoke-test-dev-dep",
                title="Add minimal Rust integration smoke test",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                description="Add a smoke test and a dev-dependency required by the test.",
                affected_files=["rust-backend/Cargo.toml", "rust-backend/src/state.rs"],
                forbidden_actions=[],
                test_requirements=["cargo test"],
            )
        ],
    )
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/lib.rs", "rust-backend/src/state.rs"]),
        FakeOllama(plan_with_dev_dep),
    )

    task = agent.plan().tasks[0]

    assert task.affected_files == ["rust-backend/tests/smoke.rs", "rust-backend/Cargo.toml"]
    assert "rust-backend/src/state.rs" not in task.affected_files


def test_inline_unit_test_request_retains_rust_source_with_higher_risk(tmp_path) -> None:
    inline_plan = TaskPlan(
        summary="Add inline unit test.",
        tasks=[
            AgentTask(
                id="rust-inline-unit-test",
                title="Add inline unit tests for Rust state",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                description="Add inline unit tests inside rust-backend/src/state.rs.",
                affected_files=["rust-backend/src/state.rs"],
                forbidden_actions=[],
                test_requirements=["cargo test"],
            )
        ],
    )
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/state.rs"]),
        FakeOllama(inline_plan),
    )

    task = agent.plan().tasks[0]

    assert task.affected_files == ["rust-backend/src/state.rs"]
    assert task.risk_level == RiskLevel.MEDIUM
    assert task.risk_score == 10
    assert task.metadata["propose_only_recommended"] is True


def test_heuristic_rust_test_baseline_uses_smoke_test_file(tmp_path) -> None:
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/lib.rs", "rust-backend/src/error.rs", "rust-backend/src/state.rs"]),
    )

    plan = agent.plan()
    task = next(item for item in plan.tasks if item.id == "add-test-baseline")

    assert task.affected_files == ["rust-backend/tests/smoke.rs"]
    assert all(not path.startswith("rust-backend/src/") for path in task.affected_files)


def test_binary_only_rust_smoke_baseline_requires_public_seam(tmp_path) -> None:
    binary_only_plan = TaskPlan(
        summary="Add Rust smoke test baseline.",
        tasks=[
            AgentTask(
                id="rust-smoke-test-baseline",
                title="Add minimal Rust smoke test baseline",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                description="Add a minimal Rust smoke test without production source changes.",
                acceptance_criteria=["A smoke test runs with cargo test."],
                affected_files=["rust-backend/tests/smoke.rs"],
                forbidden_actions=["Do not edit production source files."],
                test_requirements=["cargo test"],
            )
        ],
    )
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/main.rs"]),
        FakeOllama(binary_only_plan),
    )

    task = agent.plan().tasks[0]

    assert task.affected_files == []
    assert task.risk_level == RiskLevel.MEDIUM
    assert task.metadata["implementation_blocked_reason"] == "rust_library_seam_required"
    assert task.metadata["requires_public_library_seam"] is True
    assert "no src/lib.rs" in task.metadata["planning_note"]


def test_public_seam_request_can_plan_lib_and_integration_test(tmp_path) -> None:
    seam_plan = TaskPlan(
        summary="Add Rust public seam.",
        tasks=[
            AgentTask(
                id="rust-smoke-test-seam",
                title="Add minimal Rust smoke test with public seam",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                description="Add a minimal src/lib.rs public seam and smoke test.",
                acceptance_criteria=["A smoke test runs with cargo test."],
                affected_files=["rust-backend/tests/smoke.rs"],
                forbidden_actions=[],
                test_requirements=["cargo test"],
            )
        ],
    )
    agent = PlanningAgent(
        config(tmp_path),
        FakeFileTool(["rust-backend/Cargo.toml", "rust-backend/src/main.rs"]),
        FakeOllama(seam_plan),
    )

    task = agent.plan().tasks[0]

    assert task.affected_files == ["rust-backend/src/lib.rs", "rust-backend/tests/smoke.rs"]


def test_closed_rust_smoke_feedback_prioritizes_public_seam_over_docs(tmp_path) -> None:
    docs_plan = TaskPlan(
        summary="Docs and Rust follow-up.",
        tasks=[
            AgentTask(
                id="docs-update-test-instructions",
                title="Update test instructions",
                task_type=TaskType.DOCS,
                risk_level=RiskLevel.LOW,
                risk_score=1,
                affected_files=["README.md"],
                forbidden_actions=["Do not change code."],
            )
        ],
    )
    cfg = approval_config(tmp_path, allowed_paths=["README.md", "rust-backend/src/lib.rs", "rust-backend/tests/**"])
    agent = PlanningAgent(
        cfg,
        FakeFileTool(
            ["README.md", "rust-backend/Cargo.toml", "rust-backend/src/main.rs"],
            {"rust-backend/Cargo.toml": '[package]\nname = "zfs-manager"\nversion = "0.1.0"\nedition = "2021"\n'},
        ),
        FakeOllama(docs_plan),
        closed_agent_mr_feedback=rust_smoke_feedback(),
    )

    plan = agent.plan()
    approved_plan, report = AutoApprovalPolicy(cfg).apply(plan)

    assert plan.tasks[0].id == "rust-public-seam-smoke-test"
    assert plan.tasks[0].title == "Add minimal Rust library seam and smoke test"
    assert plan.tasks[0].affected_files == ["rust-backend/src/lib.rs", "rust-backend/tests/smoke.rs"]
    assert report["selected_task_id"] == "rust-public-seam-smoke-test"
    selected = next(task for task in approved_plan.tasks if task.id == "rust-public-seam-smoke-test")
    assert selected.approved is True


def test_closed_rust_smoke_feedback_blocks_instead_of_falling_back_to_docs_when_lib_not_allowed(tmp_path) -> None:
    docs_plan = TaskPlan(
        summary="Docs and Rust follow-up.",
        tasks=[
            AgentTask(
                id="docs-update-test-instructions",
                title="Update test instructions",
                task_type=TaskType.DOCS,
                risk_level=RiskLevel.LOW,
                risk_score=1,
                affected_files=["README.md"],
                forbidden_actions=["Do not change code."],
            )
        ],
    )
    cfg = approval_config(tmp_path, allowed_paths=["README.md", "rust-backend/tests/**"])
    agent = PlanningAgent(
        cfg,
        FakeFileTool(
            ["README.md", "rust-backend/Cargo.toml", "rust-backend/src/main.rs"],
            {"rust-backend/Cargo.toml": '[package]\nname = "zfs-manager"\nversion = "0.1.0"\nedition = "2021"\n'},
        ),
        FakeOllama(docs_plan),
        closed_agent_mr_feedback=rust_smoke_feedback(),
    )

    plan = agent.plan()
    _approved_plan, report = AutoApprovalPolicy(cfg).apply(plan)

    assert report["selected_task_id"] is None
    assert report["blocked_priority_task"]["task_id"] == "rust-public-seam-smoke-test"
    assert report["blocked_priority_task"]["hint"] == "Rust public seam task requires rust-backend/src/lib.rs to be allowed."
    rejected = next(item for item in report["rejected_tasks"] if item["task_id"] == "rust-public-seam-smoke-test")
    assert "path_not_allowed" in rejected["reasons"]
    assert rejected["details"]["policy_blocked_hint"] == "Rust public seam task requires rust-backend/src/lib.rs to be allowed."


def test_focus_hint_prioritizes_matching_test_task_over_low_risk_docs(tmp_path) -> None:
    plan = TaskPlan(
        summary="Docs and tests.",
        tasks=[
            AgentTask(
                id="docs-update-test-instructions",
                title="Update test instructions",
                task_type=TaskType.DOCS,
                risk_level=RiskLevel.LOW,
                risk_score=1,
                affected_files=["README.md"],
            ),
            AgentTask(
                id="rust-smoke-doc-test",
                title="Add Rust smoke test",
                task_type=TaskType.TESTS,
                risk_level=RiskLevel.LOW,
                risk_score=3,
                affected_files=["rust-backend/tests/smoke.rs"],
            ),
        ],
    )
    cfg = approval_config(tmp_path, allowed_paths=["README.md", "rust-backend/tests/**"])
    agent = PlanningAgent(
        cfg,
        FakeFileTool(["README.md", "rust-backend/Cargo.toml", "rust-backend/src/lib.rs"]),
        FakeOllama(plan),
        focus="rust smoke test",
    )

    _approved_plan, report = AutoApprovalPolicy(cfg).apply(agent.plan())

    assert report["selected_task_id"] == "rust-smoke-doc-test"
