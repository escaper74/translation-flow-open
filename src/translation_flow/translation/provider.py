"""Provider boundary and the current DeepSeek OpenAI-compatible adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol, Sequence

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL


Message = dict[str, str]
CONNECTION_TEST_TEXT = "はい"


class TranslationProviderError(RuntimeError):
    pass


class FatalProviderError(TranslationProviderError):
    pass


class RetryableProviderError(TranslationProviderError):
    pass


class TranslationProvider(Protocol):
    provider_id: str
    cache_identity: str
    default_model: str

    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        json_output: bool = False,
    ) -> str: ...

    def check_connection(self) -> None: ...


def normalize_chat_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    # DeepSeek's documented base accepts /chat/completions directly. Other
    # OpenAI-compatible bases conventionally use /v1/chat/completions.
    if base == "https://api.deepseek.com":
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class DeepSeekProvider:
    provider_id = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        api_base: str = "https://api.deepseek.com",
        timeout: int = 240,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        self._api_key = api_key
        self.default_model = model
        self.api_url = normalize_chat_url(api_base)
        self.cache_identity = f"{self.provider_id}:{self.api_url}"
        self.timeout = timeout
        self._opener = opener

    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        json_output: bool = False,
    ) -> str:
        request_payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
        }
        if json_output:
            request_payload.update(
                {
                    "response_format": {"type": "json_object"},
                    "max_tokens": 32768,
                }
            )
        if model.startswith("deepseek-v4-"):
            request_payload.update(
                {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                }
            )
        else:
            request_payload["temperature"] = temperature
        payload = json.dumps(
            request_payload,
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in (408, 429) and exc.code < 500:
                raise FatalProviderError(
                    f"DeepSeek rejected the request ({exc.code}): {body}"
                ) from exc
            raise RetryableProviderError(
                f"DeepSeek HTTP request failed ({exc.code}): {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise RetryableProviderError(str(exc)) from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RetryableProviderError("DeepSeek response envelope is invalid") from exc
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RetryableProviderError("DeepSeek response envelope is invalid") from exc
        if not isinstance(content, str):
            raise RetryableProviderError("DeepSeek response content is not text")
        return content

    def check_connection(self) -> None:
        messages: list[Message] = [
            {
                "role": "system",
                "content": (
                    "Translate the single Japanese subtitle into Simplified Chinese. "
                    "Return only JSON: {\"items\":[{\"id\":1,\"text\":\"...\"}]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"items": [{"id": 1, "text": CONNECTION_TEST_TEXT}]},
                    ensure_ascii=False,
                ),
            },
        ]
        content = self.chat(
            messages,
            model=self.default_model,
            temperature=0.0,
            json_output=True,
        )
        from .pipeline import parse_item_response

        result = parse_item_response(content, [1])
        if not result[1].strip():
            raise RetryableProviderError("DeepSeek connection check returned empty text")


def create_deepseek_provider(
    api_key: str,
    model: str = DEFAULT_DEEPSEEK_MODEL,
    api_base: str = "https://api.deepseek.com",
) -> DeepSeekProvider:
    return DeepSeekProvider(api_key, model=model, api_base=api_base)
