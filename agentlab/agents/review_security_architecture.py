from __future__ import annotations

import json

from agentlab.config import AppConfig
from agentlab.models import ReviewComment, ReviewReport, Verdict
from agentlab.policies.risk import detect_secret_content
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


class SecurityArchitectureReviewAgent:
    name = "review_security_architecture"

    def __init__(self, config: AppConfig, ollama: OllamaClient | None = None) -> None:
        self.config = config
        self.ollama = ollama

    def review(self, diff_text: str) -> ReviewReport:
        if self.ollama is not None:
            try:
                return self.ollama.chat_json(
                    model=self.config.agent_model("review_security"),
                    system_prompt=load_prompt("reviewer_security.md"),
                    user_prompt=json.dumps({"diff": compact_text(diff_text)}, indent=2),
                    response_model=ReviewReport,
                )
            except Exception:
                pass
        comments: list[ReviewComment] = []
        verdict = Verdict.APPROVED
        lowered = diff_text.lower()
        if detect_secret_content(diff_text):
            comments.append(ReviewComment(body="Diff appears to include secret-like content.", severity="critical"))
            verdict = Verdict.BLOCKED
        elif "privileged: true" in lowered or "--privileged" in lowered:
            comments.append(ReviewComment(body="Privileged containers are not allowed.", severity="critical"))
            verdict = Verdict.BLOCKED
        elif "dockerfile" in lowered and "user " not in lowered:
            comments.append(ReviewComment(body="Dockerfile changes should consider a non-root runtime user.", severity="medium"))
            verdict = Verdict.CHANGES_REQUESTED
        return ReviewReport(
            reviewer="security_architecture",
            verdict=verdict,
            summary="Heuristic security and architecture review completed.",
            comments=comments,
        )
