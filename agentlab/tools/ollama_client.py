from __future__ import annotations

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from agentlab.config import OllamaConfig

T = TypeVar("T", bound=BaseModel)


class OllamaSchemaValidationError(RuntimeError):
    def __init__(self, *, model_name: str, validation_error: str | None, raw_response: str) -> None:
        super().__init__(f"Ollama response failed schema validation for {model_name}: {validation_error}")
        self.model_name = model_name
        self.validation_error = validation_error or ""
        self.raw_response = raw_response


class OllamaClient:
    def __init__(self, config: OllamaConfig, *, timeout_seconds: int = 120) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def chat_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        retries: int = 2,
    ) -> T:
        parsed, _ = self.chat_json_with_raw(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=response_model,
            retries=retries,
        )
        return parsed

    def chat_json_with_raw(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        retries: int = 2,
    ) -> tuple[T, str]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: str | None = None
        last_content = ""
        for attempt in range(retries + 1):
            content = self._chat(model=model, messages=messages)
            last_content = content
            try:
                return response_model.model_validate_json(content), content
            except ValidationError as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required JSON schema. "
                            f"Return only valid JSON for {response_model.__name__}. Validation error: {last_error}"
                        ),
                    }
                )
        raise OllamaSchemaValidationError(
            model_name=response_model.__name__,
            validation_error=last_error,
            raw_response=last_content,
        )

    def _chat(self, *, model: str, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(f"{self.config.base_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Ollama response did not include message.content")
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama did not return JSON content") from exc
        return content
