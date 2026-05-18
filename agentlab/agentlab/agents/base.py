from __future__ import annotations

from pathlib import Path


def load_prompt(name: str) -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / name
    return prompt_path.read_text(encoding="utf-8")


def compact_text(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit // 2] + "\n...<truncated>...\n" + value[-limit // 2 :]
