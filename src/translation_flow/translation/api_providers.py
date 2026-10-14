"""Text-only API adapters shared by SRT and video translation entrypoints.

DeepSeek keeps its production adapter and cache identity. Other providers use
their documented wire protocol with model defaults for sampling/thinking; no
model fallback, tools, media upload, or extra retry layer is introduced here.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL, SUPPORTED_DEEPSEEK_MODELS

from .provider import (
    DeepSeekProvider, FatalProviderError, Message, RetryableProviderError,
    TranslationProvider,
)


@dataclass(frozen=True)
class ApiEndpoint:
    provider: str
    protocol: str
    url: str


# Named official endpoints keep consent tied to a real service. Regional keys
# are not interchangeable. No arbitrary proxy URL or credential in CLI args.
ENDPOINTS = {
    "deepseek": ApiEndpoint("deepseek", "chat", "https://api.deepseek.com/chat/completions"),
    "qwen-cn": ApiEndpoint("qwen", "chat", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "qwen-intl": ApiEndpoint("qwen", "chat", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"),
    "kimi-cn": ApiEndpoint("kimi", "chat", "https://api.moonshot.cn/v1/chat/completions"),
    "kimi-intl": ApiEndpoint("kimi", "chat", "https://api.moonshot.ai/v1/chat/completions"),
    "openai": ApiEndpoint("openai", "responses", "https://api.openai.com/v1/responses"),
    "anthropic": ApiEndpoint("anthropic", "messages", "https://api.anthropic.com/v1/messages"),
    "gemini": ApiEndpoint("gemini", "chat", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
}
DEFAULT_ENDPOINTS = {
    "deepseek": "deepseek", "qwen": "qwen-cn", "kimi": "kimi-cn",
    "openai": "openai", "anthropic": "anthropic", "gemini": "gemini",
}
API_PROVIDERS = tuple(DEFAULT_ENDPOINTS)


def resolve_endpoint(provider_id: str, endpoint: str | None = None) -> ApiEndpoint:
    if provider_id not in DEFAULT_ENDPOINTS:
        raise ValueError(f"Unsupported API provider: {provider_id}")
    selected = ENDPOINTS.get(endpoint or DEFAULT_ENDPOINTS[provider_id])
    if selected is None or selected.provider != provider_id:
        raise ValueError("Endpoint profile does not belong to the selected provider")
    return selected


def resolve_model(provider_id: str, model: str | None) -> str:
    resolve_endpoint(provider_id)
    if provider_id == "deepseek":
        model = model or DEFAULT_DEEPSEEK_MODEL
        if model not in SUPPORTED_DEEPSEEK_MODELS:
            raise ValueError("DeepSeek model must be one of: " + ", ".join(sorted(SUPPORTED_DEEPSEEK_MODELS)))
    if not model or not model.strip():
        raise ValueError(f"Select an explicit model ID for {provider_id}; no automatic model choice")
    if model != model.strip() or any(ord(char) < 32 for char in model):
        raise ValueError("Model ID contains whitespace/control characters")
    return model


def api_cache_identity(provider_id: str, endpoint: str | None, max_output_tokens: int) -> str:
    """Public configuration identity, also usable for keyless checkpoint reuse."""
    selected = resolve_endpoint(provider_id, endpoint)
    if provider_id == "deepseek":
        return f"deepseek:{selected.url}"
    return (f"{provider_id}:{selected.url}:text-api-v1:"
            f"max_output_tokens={max_output_tokens}:sampling=provider-default")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A provider response must not forward this run's credential/text to a
        # different destination. Normal endpoint POSTs do not need redirects.
        return None


class HttpApiProvider:
    def __init__(
        self, api_key: str, *, provider_id: str, model: str,
        endpoint: str | None = None, max_output_tokens: int = 8192,
        timeout: int = 240, opener: Callable[..., Any] | None = None,
    ) -> None:
        selected = resolve_endpoint(provider_id, endpoint)
        if provider_id == "deepseek":
            raise ValueError("Use the existing DeepSeek adapter")
        if not api_key.strip():
            raise ValueError(f"{provider_id} API key is required")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.provider_id = provider_id
        self.default_model = resolve_model(provider_id, model)
        self.api_url = selected.url
        self.protocol = selected.protocol
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self._api_key = api_key
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.cache_identity = api_cache_identity(provider_id, endpoint, max_output_tokens)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.protocol == "messages":
            headers.update({"x-api-key": self._api_key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self.api_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never expose an error body: services can echo keys or source text.
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                body = {}
            error = body.get("error", {}) if isinstance(body, dict) else {}
            codes = []
            if isinstance(error, dict):
                codes.extend([error.get("type"), error.get("code")])
                details = error.get("details")
                if isinstance(details, dict):
                    codes.append(details.get("error_code"))
            quota = any(code in (
                "billing_hard_limit_reached",
                "enforced_spend_limit_reached", "insufficient_balance",
            ) for code in codes if isinstance(code, str))
            # Qwen also uses insufficient_quota for temporary TPS/TPM limits;
            # do not transfer OpenAI's balance semantics to every compatible API.
            quota = quota or (self.provider_id == "openai" and "insufficient_quota" in codes)
            diagnostic = f"{self.provider_id} HTTP {exc.code}"
            if quota:
                raise FatalProviderError(diagnostic + ": account quota/balance exhausted; not retried") from None
            if exc.code in (408, 429) or 500 <= exc.code < 600:
                failure = RetryableProviderError(diagnostic + ": temporary service failure")
                retry_after = (exc.headers or {}).get("Retry-After", "")
                if re.fullmatch(r"\d+(?:\.\d+)?", retry_after):
                    delay = float(retry_after)
                    if delay > 300:
                        raise FatalProviderError(diagnostic + ": retry later (server backoff exceeds 5 minutes)") from None
                    failure.retry_after_seconds = delay
                raise failure from None
            raise FatalProviderError(diagnostic + ": check credentials, endpoint and model parameters") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise RetryableProviderError(f"{self.provider_id} network request failed") from None
        except (ValueError, UnicodeError):
            raise RetryableProviderError(f"{self.provider_id} returned an invalid JSON envelope") from None
        if not isinstance(result, dict):
            raise RetryableProviderError(f"{self.provider_id} response must be an object")
        if result.get("error"):
            raise FatalProviderError(f"{self.provider_id} returned an error envelope; no text accepted")
        return result

    def chat(
        self, messages: Sequence[Message], *, model: str,
        temperature: float, json_output: bool = False,
    ) -> str:
        if model != self.default_model:
            raise FatalProviderError("Model differs from the explicitly configured API model")
        # Preserve the existing prompts exactly. Restrict this boundary to text,
        # not OpenAI-compatible image/audio/tool message variants.
        copied = []
        for message in messages:
            if message.get("role") not in ("system", "user", "assistant") or not isinstance(message.get("content"), str):
                raise FatalProviderError("Subtitle API accepts only system/user/assistant text messages")
            copied.append({"role": message["role"], "content": message["content"]})
        payload: dict[str, Any] = {"model": model, "stream": False}
        if self.protocol == "responses":
            payload.update(input=copied, store=False, max_output_tokens=self.max_output_tokens)
            if json_output:
                payload["text"] = {"format": {"type": "json_object"}}
        elif self.protocol == "messages":
            payload.update(
                system="\n\n".join(m["content"] for m in copied if m["role"] == "system"),
                messages=[m for m in copied if m["role"] != "system"],
                max_tokens=self.max_output_tokens,
            )
        else:
            payload.update(messages=copied, max_tokens=self.max_output_tokens)
            # JSON comes from the existing prompt/parser contract. Do not force
            # JSON mode on thinking models that do not support that combination.
        result = self._post(payload)
        try:
            if self.protocol == "responses":
                content = self._responses_text(result)
            elif self.protocol == "messages":
                content = self._messages_text(result)
            else:
                content = self._chat_text(result)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise RetryableProviderError(f"{self.provider_id} response envelope is invalid") from None
        if not isinstance(content, str) or not content.strip():
            raise RetryableProviderError(f"{self.provider_id} returned no usable text")
        return content

    def _incomplete(self, reason: str) -> None:
        raise FatalProviderError(
            f"{self.provider_id} did not complete its response ({reason}); "
            "nothing published. For token limits, reduce batch size or increase --max-output-tokens."
        )

    def _chat_text(self, result: dict[str, Any]) -> str:
        choice = result["choices"][0]
        message = choice["message"]
        reason = choice.get("finish_reason")
        if message.get("refusal") or (isinstance(reason, str) and reason.startswith("content_filter")):
            self._incomplete("refusal/content filter")
        if reason != "stop" or message.get("tool_calls") or message.get("function_call"):
            self._incomplete("token limit" if reason == "length" else "unexpected finish state")
        return message["content"]

    def _messages_text(self, result: dict[str, Any]) -> str:
        details = result.get("stop_details") or {}
        reason = result.get("stop_reason")
        if reason == "refusal" or details.get("type") == "refusal":
            self._incomplete("refusal")
        if reason != "end_turn":
            self._incomplete("token limit" if reason in ("max_tokens", "model_context_window_exceeded") else "unexpected finish state")
        blocks = result["content"]
        if any(block.get("type") not in ("text", "thinking", "redacted_thinking") for block in blocks):
            self._incomplete("unexpected content type")
        return "".join(block["text"] for block in blocks if block.get("type") == "text")

    def _responses_text(self, result: dict[str, Any]) -> str:
        if result.get("status") != "completed":
            details = result.get("incomplete_details") or {}
            self._incomplete("token limit" if details.get("reason") == "max_output_tokens" else "unexpected finish state")
        parts = []
        for item in result["output"]:
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("status") != "completed":
                self._incomplete("unexpected output item")
            for block in item["content"]:
                if block.get("type") != "output_text":
                    self._incomplete("refusal" if block.get("type") == "refusal" else "unexpected content type")
                parts.append(block["text"])
        return "".join(parts)

    def check_connection(self) -> None:
        # Same small text-only probe, with no dependency on production media.
        DeepSeekProvider.check_connection(self)


def create_api_provider(
    provider_id: str, api_key: str, *, model: str | None = None,
    endpoint: str | None = None, max_output_tokens: int | None = None,
    timeout: int = 240, opener: Callable[..., Any] | None = None,
) -> TranslationProvider:
    selected = resolve_endpoint(provider_id, endpoint)
    selected_model = resolve_model(provider_id, model)
    if provider_id == "deepseek":
        if max_output_tokens is not None:
            raise ValueError("DeepSeek retains its existing token settings; omit --max-output-tokens")
        kwargs: dict[str, Any] = {"model": selected_model, "timeout": timeout}
        if opener is not None:
            kwargs["opener"] = opener
        return DeepSeekProvider(api_key, **kwargs)
    return HttpApiProvider(
        api_key, provider_id=selected.provider, model=selected_model,
        endpoint=endpoint, max_output_tokens=8192 if max_output_tokens is None else max_output_tokens,
        timeout=timeout, opener=opener,
    )
