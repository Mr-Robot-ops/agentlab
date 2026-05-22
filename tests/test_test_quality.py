from __future__ import annotations

from pathlib import Path

from agentlab.agents.test_functional import FunctionalTestAgent
from agentlab.agents.test_quality import TestQualityAgent as QualityAgent
from agentlab.config import AppConfig
from agentlab.models import ReportStatus
from agentlab.tools.file_tool import FileTool
from agentlab.tools.test_tool import TestTool as AgentTestTool


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        gitlab_url="https://gitlab.example.com",
        project_id=1,
        target_repo_path=tmp_path,
        workspace_root=tmp_path / "runs",
        allowed_commands=["cargo test"],
        forbidden_commands=[],
    )


def write_rust_backend(repo: Path, test_content: str, *, package_name: str = "rust-backend") -> None:
    (repo / "rust-backend" / "tests").mkdir(parents=True)
    (repo / "rust-backend" / "src").mkdir(parents=True)
    (repo / "rust-backend" / "Cargo.toml").write_text(
        f'[package]\nname = "{package_name}"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (repo / "rust-backend" / "src" / "lib.rs").write_text(
        "pub mod routes { pub fn health_path() -> &'static str { \"/health\" } }\n",
        encoding="utf-8",
    )
    (repo / "rust-backend" / "tests" / "smoke.rs").write_text(test_content, encoding="utf-8")


def report_for(repo: Path, test_content: str):
    write_rust_backend(repo, test_content)
    return QualityAgent(FileTool(repo, config(repo))).run(["rust-backend/tests/smoke.rs"])


def test_rust_assert_true_is_blocked_by_test_quality_report(tmp_path: Path) -> None:
    report = report_for(
        tmp_path,
        "#[test]\nfn test_smoke() {\n    assert!(true);\n}\n",
    )

    assert report.status == ReportStatus.FAILED
    assert report.reason == "placeholder_test_detected"
    assert report.findings[0].path == "rust-backend/tests/smoke.rs"
    assert report.findings[0].line == 3
    assert report.findings[0].reason == "assert_true"


def test_rust_async_assert_true_with_message_is_blocked(tmp_path: Path) -> None:
    report = report_for(
        tmp_path,
        "#[tokio::test]\nasync fn test_smoke() {\n    assert!(true, \"framework runs\");\n}\n",
    )

    assert report.status == ReportStatus.FAILED
    assert report.findings[0].reason == "assert_true"


def test_rust_literal_assertion_is_blocked(tmp_path: Path) -> None:
    report = report_for(
        tmp_path,
        "#[test]\nfn test_smoke() {\n    assert_eq!(1, 1);\n}\n",
    )

    assert report.status == ReportStatus.FAILED
    assert any(finding.reason == "literal_assertion" for finding in report.findings)


def test_rust_test_without_project_specific_behavior_is_blocked(tmp_path: Path) -> None:
    report = report_for(
        tmp_path,
        "#[test]\nfn parses_number() {\n    let parsed = \"1\".parse::<u32>().unwrap();\n    assert_eq!(parsed, 1);\n}\n",
    )

    assert report.status == ReportStatus.FAILED
    assert any(finding.reason == "no_project_behavior" for finding in report.findings)


def test_meaningful_rust_test_is_allowed(tmp_path: Path) -> None:
    report = report_for(
        tmp_path,
        "use rust_backend::routes;\n\n#[test]\nfn health_path_is_exposed() {\n    assert_eq!(routes::health_path(), \"/health\");\n}\n",
    )

    assert report.status == ReportStatus.PASSED
    assert report.findings == []


def test_rust_backend_changed_test_file_triggers_cargo_test_command(tmp_path: Path) -> None:
    write_rust_backend(
        tmp_path,
        "use rust_backend::routes;\n\n#[test]\nfn health_path_is_exposed() {\n    assert_eq!(routes::health_path(), \"/health\");\n}\n",
    )
    file_tool = FileTool(tmp_path, config(tmp_path))
    test_tool = AgentTestTool(tmp_path, config(tmp_path))

    commands = FunctionalTestAgent(
        file_tool,
        test_tool,
        changed_files=["rust-backend/tests/smoke.rs"],
    ).detect_commands()

    assert commands == ["cd rust-backend && cargo test --package rust-backend"]


def test_safe_cd_test_command_is_allowlisted_by_inner_command(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write_rust_backend(tmp_path, "#[test]\nfn test_smoke() {}\n")
    tool = AgentTestTool(tmp_path, cfg)

    assert tool.is_allowed("cd rust-backend && cargo test --package rust-backend")
