from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMClientError(RuntimeError):
    """Raised when an LLM backend fails, times out, or returns invalid structured output."""


@runtime_checkable
class LLMClient(Protocol):
    """Replaceable interface for structured-output LLM providers."""

    model_name: str

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T: ...


class OllamaClient:
    """Local Ollama HTTP client calling /api/chat with Pydantic JSON schema enforcement."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model_name: str = "qwen2.5:7b",
        *,
        timeout_seconds: float = 60.0,
        num_ctx: int = 8192,
        temperature: float = 0.1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        cleaned_url = base_url.strip().rstrip("/")
        cleaned_model = model_name.strip()
        if not cleaned_url:
            raise ValueError("base_url cannot be empty")
        if not cleaned_model:
            raise ValueError("model_name cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if num_ctx < 512:
            raise ValueError("num_ctx must be at least 512")

        self.base_url = cleaned_url
        self.model_name = cleaned_model
        self.timeout_seconds = float(timeout_seconds)
        self.num_ctx = int(num_ctx)
        self.temperature = float(temperature)
        self._transport = transport

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": response_model.model_json_schema(),
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
            },
        }

        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMClientError(
                f"Ollama request timed out after {self.timeout_seconds}s "
                f"for model {self.model_name}"
            ) from exc
        except httpx.ConnectError as exc:
            raise LLMClientError(
                f"Unable to connect to Ollama service for model {self.model_name}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMClientError(
                f"HTTP error communicating with Ollama ({type(exc).__name__})"
            ) from exc

        if response.status_code != 200:
            raise LLMClientError(
                f"Ollama returned HTTP {response.status_code} for model {self.model_name}"
            )

        try:
            outer = response.json()
        except ValueError as exc:
            raise LLMClientError("Ollama returned a non-JSON response payload") from exc

        message = outer.get("message") if isinstance(outer, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMClientError("Ollama response missing structured message content")

        try:
            return response_model.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise LLMClientError(
                f"Ollama structured output failed validation for {response_model.__name__}"
            ) from exc


class FakeLLMClient:
    """Deterministic in-memory LLM double for unit and workflow tests."""

    def __init__(
        self,
        response: BaseModel | dict[str, Any] | Callable[[str, str, type[Any]], Any] | None = None,
        *,
        error: Exception | None = None,
        model_name: str = "fake-llm",
    ) -> None:
        self.model_name = model_name
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_model": response_model.__name__,
            }
        )

        if self._error is not None:
            if isinstance(self._error, LLMClientError):
                raise self._error
            raise LLMClientError(str(self._error)) from self._error

        if self._response is None:
            raise LLMClientError("FakeLLMClient has no configured response")

        result = (
            self._response(system_prompt, user_prompt, response_model)
            if callable(self._response)
            else self._response
        )

        if isinstance(result, response_model):
            return result
        if isinstance(result, BaseModel):
            return response_model.model_validate(result.model_dump())
        if isinstance(result, str):
            try:
                return response_model.model_validate_json(result)
            except (ValidationError, ValueError) as exc:
                raise LLMClientError(f"Invalid fake JSON response: {exc}") from exc
        if isinstance(result, dict):
            try:
                return response_model.model_validate(result)
            except ValidationError as exc:
                raise LLMClientError(f"Invalid fake dict response: {exc}") from exc

        raise LLMClientError(
            f"FakeLLMClient returned unsupported type {type(result).__name__}"
        )
