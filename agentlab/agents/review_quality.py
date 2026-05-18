from __future__ import annotations

import json

from agentlab.config import AppConfig
from agentlab.models import ReviewComment, ReviewReport, Verdict
from agentlab.tools.ollama_client import OllamaClient

from .base import compact_text, load_prompt


class CodeQualityReviewAgent:
    name = "review_quality"

    def __init__(self, config: AppConfig, ollama: OllamaClient | None = None) -> None:
        self.config = config
        self.ollama = ollama

    def review(self, diff_text: str) -> ReviewReport:
        if self.ollama is not None:
            try:
                return self.ollama.chat_json(
                    model=self.config.agent_model("review_quality"),
                    system_prompt=load_prompt("reviewer_quality.md"),
                    user_prompt=json.dumps({"diff": compact_text(diff_text)}, indent=2),
                    response_model=ReviewReport,
                )
            except Exception:
                pass
        comments: list[ReviewComment] = []
        if len(diff_text.splitlines()) > self.config.max_added_lines + self.config.max_deleted_lines:
            comments.append(ReviewComment(body="Diff is large; split into smaller changes.", severity="high"))
        verdict = Verdict.CHANGES_REQUESTED if comments else Verdict.APPROVED
        return ReviewReport(reviewer="quality", verdict=verdict, summary="Heuristic quality review completed.", comments=comments)
