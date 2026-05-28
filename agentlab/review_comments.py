from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


ALLOWED_REVIEW_COMMANDS = {
    "revise",
    "fix",
    "propose",
    "apply",
    "dry-run",
    "status",
    "merge-status",
    "explain",
    "stop",
    "resume",
}
DENIED_REVIEW_COMMANDS = {
    "run",
    "shell",
    "bash",
    "exec",
    "deploy",
    "merge",
    "approve",
    "auto-merge",
    "push-main",
}

COMMAND_RE = re.compile(r"^\s*(?P<prefix>/agent|@agentlab)\s+(?P<command>[a-z][a-z-]*)\b(?P<tail>.*)\Z", re.IGNORECASE | re.DOTALL)
DRY_RUN_FLAG_RE = re.compile(r"(?:^|\s)--dry-run(?:\s|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedReviewCommand:
    prefix: str
    command: str
    feedback: str
    allowed: bool
    reason: str | None = None
    propose_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_review_command(
    body: str,
    *,
    allowed_commands: list[str] | set[str] | tuple[str, ...] | None = None,
) -> ParsedReviewCommand | None:
    match = COMMAND_RE.match(body or "")
    if match is None:
        return None

    allowed_set = {command.lower() for command in (allowed_commands or ALLOWED_REVIEW_COMMANDS)}
    command = match.group("command").lower()
    tail = match.group("tail") or ""
    feedback = tail.strip()
    propose_only = command in {"propose", "dry-run"}
    if command in {"revise", "fix"} and DRY_RUN_FLAG_RE.search(feedback):
        propose_only = True
        feedback = DRY_RUN_FLAG_RE.sub(" ", feedback).strip()

    if command in DENIED_REVIEW_COMMANDS or command not in allowed_set:
        return ParsedReviewCommand(
            prefix=match.group("prefix").lower(),
            command=command,
            feedback=feedback,
            allowed=False,
            reason="command_not_allowed",
            propose_only=propose_only,
        )

    return ParsedReviewCommand(
        prefix=match.group("prefix").lower(),
        command=command,
        feedback=feedback,
        allowed=True,
        propose_only=propose_only,
    )


def get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    if hasattr(value, key):
        return getattr(value, key)
    if hasattr(value, "asdict"):
        try:
            return value.asdict().get(key, default)
        except Exception:
            return default
    return default


def author_username(author: Any) -> str | None:
    username = get_value(author, "username")
    if username:
        return str(username)
    return None


def author_id(author: Any) -> int | None:
    raw = get_value(author, "id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_bot_author(author: Any, current_user: Any | None) -> bool:
    if current_user is None:
        return False
    current_id = author_id(current_user)
    if current_id is not None and author_id(author) == current_id:
        return True
    current_username = author_username(current_user)
    username = author_username(author)
    return bool(current_username and username and current_username.lower() == username.lower())


def review_comment_key(project_id: int | str, mr_iid: int, note_id: int | str) -> str:
    return f"{project_id}:{mr_iid}:{note_id}"


def mr_key(project_id: int | str, mr_iid: int) -> str:
    return f"{project_id}:{mr_iid}"


def flatten_merge_request_comments(notes: list[Any], discussions: list[Any]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for note in notes:
        normalized = normalize_note(note)
        if normalized is not None:
            normalized["source"] = "note"
            comments.append(normalized)

    for discussion in discussions:
        raw_notes = get_value(discussion, "notes", [])
        if not isinstance(raw_notes, list):
            continue
        discussion_id = get_value(discussion, "id")
        for note in raw_notes:
            normalized = normalize_note(note)
            if normalized is not None:
                normalized["source"] = "discussion"
                normalized["discussion_id"] = discussion_id
                comments.append(normalized)

    return sorted(comments, key=_comment_sort_key)


def normalize_note(note: Any) -> dict[str, Any] | None:
    note_id = get_value(note, "id")
    body = get_value(note, "body")
    if note_id is None or body is None:
        return None
    return {
        "id": note_id,
        "body": str(body),
        "author": get_value(note, "author", {}),
        "created_at": get_value(note, "created_at"),
        "updated_at": get_value(note, "updated_at"),
        "system": bool(get_value(note, "system", False)),
        "resolvable": bool(get_value(note, "resolvable", False)),
    }


def normalize_mr(mr: Any) -> dict[str, Any]:
    labels = get_value(mr, "labels", []) or []
    if isinstance(labels, str):
        labels = [label.strip() for label in labels.split(",") if label.strip()]
    return {
        "mr_id": get_value(mr, "mr_id", get_value(mr, "id")),
        "iid": get_value(mr, "iid", get_value(mr, "mr_id", get_value(mr, "id"))),
        "title": get_value(mr, "title", ""),
        "web_url": get_value(mr, "web_url"),
        "source_branch": get_value(mr, "source_branch", ""),
        "target_branch": get_value(mr, "target_branch", ""),
        "labels": list(labels),
        "state": get_value(mr, "state", "opened"),
        "description": get_value(mr, "description", ""),
        "updated_at": get_value(mr, "updated_at"),
        "closed_at": get_value(mr, "closed_at"),
        "merged_at": get_value(mr, "merged_at"),
    }


def is_agent_generated_mr(mr: Any, *, default_branch: str) -> bool:
    normalized = normalize_mr(mr)
    labels = {str(label) for label in normalized["labels"]}
    return (
        normalized["state"] in {"opened", "open", None}
        and str(normalized["source_branch"]).startswith("agent/")
        and normalized["target_branch"] == default_branch
        and "agent/generated" in labels
    )


def _comment_sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    raw_id = comment.get("id")
    try:
        note_id = int(raw_id)
    except (TypeError, ValueError):
        note_id = 0
    return str(comment.get("created_at") or ""), note_id
