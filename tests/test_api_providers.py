from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL
from translation_flow.srt import SubtitleCue, parse_srt, validate_timeline, write_srt
from translation_flow.translation.api_providers import (
    API_PROVIDERS, ENDPOINTS, HttpApiProvider, _NoRedirect,
    create_api_provider, resolve_endpoint, resolve_model,
)
from translation_flow.translation.pipeline import _chat_with_retries, cache_namespace, run_translation
from translation_flow.translation.profiles import get_profile
from translation_flow.translation.provider import (
    DeepSeekProvider, FatalProviderError, RetryableProviderError,
)
from translation_flow.translation.cli import main as cli_main


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def envelope(provider, text):
    if provider == "openai":
        return {"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "status": "completed", "content": [
                {"type": "output_text", "text": text},
            ]},
        ]}
    if provider == "anthropic":
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


class AdapterTests(unittest.TestCase):
    messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "例文"}]

    def provider(self, provider_id, payload=None, **kwargs):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return Response(envelope(provider_id, "正常") if payload is None else payload)

        model = DEFAULT_DEEPSEEK_MODEL if provider_id == "deepseek" else "explicit-test-model"
        result = create_api_provider(provider_id, "secret-fixture", model=model, opener=opener, **kwargs)
        return result, requests

    def call(self, provider):
        return provider.chat(self.messages, model=provider.default_model, temperature=0.25, json_output=True)

    def test_six_providers_use_expected_protocol_and_never_embed_key_in_body(self):
        for name in API_PROVIDERS:
            with self.subTest(provider=name):
                provider, requests = self.provider(name)
                self.assertEqual("正常", self.call(provider))
                request = requests[0]
                body = json.loads(request.data)
                self.assertNotIn("secret-fixture", request.data.decode())
                self.assertEqual(resolve_endpoint(name).url, request.full_url)
                if name == "deepseek":
                    self.assertIsInstance(provider, DeepSeekProvider)
                    self.assertEqual("deepseek:https://api.deepseek.com/chat/completions", provider.cache_identity)
                    self.assertEqual({"type": "enabled"}, body["thinking"])
                    self.assertEqual(32768, body["max_tokens"])
                else:
                    self.assertNotIn("temperature", body)
                    self.assertNotIn("thinking", body)
                    self.assertNotIn("tools", body)
                    self.assertFalse(body["stream"])
                    if name == "openai":
                        self.assertFalse(body["store"])
                        self.assertEqual(self.messages, body["input"])
                        self.assertEqual({"format": {"type": "json_object"}}, body["text"])
                        self.assertEqual(8192, body["max_output_tokens"])
                    elif name == "anthropic":
                        self.assertEqual("Return JSON.", body["system"])
                        self.assertEqual([self.messages[1]], body["messages"])
                        self.assertEqual("2023-06-01", request.get_header("Anthropic-version"))
                        self.assertEqual("secret-fixture", request.get_header("X-api-key"))
                    else:
                        self.assertEqual(self.messages, body["messages"])
                        self.assertNotIn("response_format", body)
                        self.assertEqual(8192, body["max_tokens"])
                if name != "anthropic":
                    self.assertEqual("Bearer secret-fixture", request.get_header("Authorization"))

    def test_text_blocks_are_joined_but_reasoning_is_not_translation(self):
        provider, _ = self.provider("anthropic", {"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": "not subtitle"},
            {"type": "text", "text": "第一"}, {"type": "text", "text": "第二"},
        ]})
        self.assertEqual("第一第二", self.call(provider))
        payload = envelope("openai", "第一")
        payload["output"][1]["content"].append({"type": "output_text", "text": "第二"})
        provider, _ = self.provider("openai", payload)
        self.assertEqual("第一第二", self.call(provider))

    def test_refusals_truncation_and_tool_outputs_stop_without_returning_partial(self):
        for name in ("qwen", "kimi", "gemini"):
            for reason in ("length", "content_filter", "content_filter: SAFETY", "tool_calls", None, "unknown"):
                payload = envelope(name, "partial")
                payload["choices"][0]["finish_reason"] = reason
                with self.subTest(provider=name, reason=reason), self.assertRaises(FatalProviderError):
                    self.call(self.provider(name, payload)[0])
            payload = envelope(name, "partial")
            payload["choices"][0]["message"]["refusal"] = "refused"
            with self.assertRaises(FatalProviderError):
                self.call(self.provider(name, payload)[0])
        for reason in ("max_tokens", "model_context_window_exceeded", "refusal", "tool_use", "pause_turn", None):
            payload = envelope("anthropic", "partial")
            payload["stop_reason"] = reason
            with self.subTest(reason=reason), self.assertRaises(FatalProviderError):
                self.call(self.provider("anthropic", payload)[0])
        for change in ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, {"status": "failed"}):
            payload = envelope("openai", "partial") | change
            with self.assertRaises(FatalProviderError):
                self.call(self.provider("openai", payload)[0])
        payload = envelope("openai", "partial")
        payload["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
        with self.assertRaises(FatalProviderError):
            self.call(self.provider("openai", payload)[0])
        payload = envelope("anthropic", "partial") | {"stop_details": {"type": "refusal"}}
        with self.assertRaises(FatalProviderError):
            self.call(self.provider("anthropic", payload)[0])

    def test_malformed_and_empty_envelopes_are_not_success(self):
        for name in API_PROVIDERS[1:]:
            for payload in ({}, [], envelope(name, ""), envelope(name, None)):
                with self.subTest(provider=name, payload=payload), self.assertRaises((FatalProviderError, RetryableProviderError)):
                    self.call(self.provider(name, payload)[0])

    def test_http_failure_classes_redact_body_and_honor_quota(self):
        cases = [(400, {}, FatalProviderError), (401, {}, FatalProviderError),
                 (402, {}, FatalProviderError), (408, {}, RetryableProviderError),
                 (429, {}, RetryableProviderError), (529, {}, RetryableProviderError),
                 (429, {"details": {"error_code": "enforced_spend_limit_reached"}}, FatalProviderError)]
        for code, error, error_class in cases:
            def opener(request, timeout):
                body = json.dumps({"error": error | {"message": "secret-fixture and private source text"}}).encode()
                raise urllib.error.HTTPError(request.full_url, code, "unsafe", {"Retry-After": "12"}, io.BytesIO(body))
            provider = create_api_provider("anthropic", "secret-fixture", model="explicit-model", opener=opener)
            with self.subTest(code=code, error=error), self.assertRaises(error_class) as caught:
                self.call(provider)
            self.assertNotIn("secret-fixture", str(caught.exception))
            self.assertNotIn("private source text", str(caught.exception))
            if error_class is RetryableProviderError:
                self.assertEqual(12, caught.exception.retry_after_seconds)

    def test_qwen_allocation_quota_is_transient_but_openai_quota_is_balance(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "quota", {"Retry-After": "3"}, io.BytesIO(
                b'{"error":{"code":"insufficient_quota","type":"Throttling.AllocationQuota"}}'
            ))
        for name, failure in (("qwen", RetryableProviderError), ("openai", FatalProviderError)):
            with self.subTest(provider=name), self.assertRaises(failure):
                self.call(create_api_provider(name, "key", model="explicit-model", opener=opener))

    def test_configuration_rejects_cross_provider_endpoint_and_implicit_model(self):
        for name in API_PROVIDERS[1:]:
            with self.assertRaises(ValueError):
                create_api_provider(name, "key")
        with self.assertRaises(ValueError):
            resolve_endpoint("kimi", "qwen-cn")
        with self.assertRaises(ValueError):
            create_api_provider("deepseek", "key", max_output_tokens=8192)
        with self.assertRaises(ValueError):
            resolve_model("deepseek", "deepseek-chat")
        for cap in (0, -1, True):
            with self.assertRaises(ValueError):
                self.provider("openai", max_output_tokens=cap)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_cache_isolates_provider_endpoint_model_and_options_not_key(self):
        def namespace(provider, model=None):
            return cache_namespace(Path("cache"), profile=get_profile("normal"), provider=provider,
                                   model=model or provider.default_model, source_digest="source", pipeline_options={})
        all_paths = {namespace(self.provider(name)[0]) for name in API_PROVIDERS}
        self.assertEqual(6, len(all_paths))
        provider, _ = self.provider("qwen")
        other_key = create_api_provider("qwen", "different-key", model=provider.default_model)
        self.assertEqual(namespace(provider), namespace(other_key))
        self.assertNotEqual(namespace(provider), namespace(provider, "other-model"))
        self.assertNotEqual(namespace(provider), namespace(self.provider("qwen", endpoint="qwen-intl")[0]))
        self.assertNotEqual(namespace(provider), namespace(self.provider("qwen", max_output_tokens=16000)[0]))


class PipelineApiTests(unittest.TestCase):
    def test_one_retry_layer_honors_bounded_server_delay_without_changing_legacy(self):
        for server_delay, expected in ((12, 12), (None, 2), (0, 2)):
            failure = RetryableProviderError("temporary")
            if server_delay is not None:
                failure.retry_after_seconds = server_delay
            # This legacy provider has no cooperative wait hook.
            provider = mock.Mock(spec=["chat"])
            provider.chat.side_effect = [failure, "result"]
            with self.subTest(delay=server_delay), mock.patch("translation_flow.translation.pipeline.time.sleep") as sleep:
                result = _chat_with_retries(provider, [], model="model", temperature=0.25, retries=2)
                self.assertEqual("result", result)
                self.assertEqual(2, provider.chat.call_count)
                sleep.assert_called_once_with(expected)

    def test_all_six_use_existing_context_two_lanes_polish_and_valid_srt(self):
        for name in API_PROVIDERS:
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = write_srt(root / "source.srt", [SubtitleCue(i, i * 1000, i * 1000 + 900, "おはよう") for i in range(1, 5)])
                calls = []

                def opener(request, timeout):
                    body = json.loads(request.data)
                    messages = body.get("input", body.get("messages"))
                    system = body.get("system", messages[0]["content"])
                    user = messages[-1]["content"]
                    calls.append(system)
                    if system.startswith("为日语") or system.startswith("合并分段"):
                        return Response(envelope(name, "问候场景"))
                    items = json.loads(user[user.rfind('{"items"'):])["items"]
                    return Response(envelope(name, json.dumps({"items": [{"id": i["id"], "text": "早上好"} for i in items]}, ensure_ascii=False)))

                model = DEFAULT_DEEPSEEK_MODEL if name == "deepseek" else "explicit-model"
                provider = create_api_provider(name, "secret-fixture", model=model, opener=opener)
                report = run_translation(source, root / "zh.srt", root / "report.json", root / "cache", "normal", provider, model, batch_size=1, retries=1)
                self.assertEqual(4, report["translated_count"])
                self.assertEqual(name, report["provider"])
                self.assertEqual(2, report["concurrency"])
                self.assertEqual(9, len(calls))
                self.assertTrue(all(x.startswith("对照日文润色") for x in calls[-4:]))
                validate_timeline(parse_srt(source), parse_srt(root / "zh.srt"))
                before = list(calls)
                run_translation(source, root / "again.srt", root / "again.json", root / "cache", "normal", provider, model, batch_size=1, retries=1)
                self.assertEqual(before, calls)
                persisted = "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())
                self.assertNotIn("secret-fixture", persisted)

    def test_failed_batch_preserves_success_and_resume_only_fills_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = write_srt(root / "source.srt", [SubtitleCue(i, i * 1000, i * 1000 + 900, "おはよう") for i in range(1, 4)])
            called_ids = []
            fail = True

            def opener(request, timeout):
                nonlocal fail
                user = json.loads(request.data)["messages"][-1]["content"]
                items = json.loads(user[user.rfind('{"items"'):])["items"]
                ids = [item["id"] for item in items]
                called_ids.extend(ids)
                if fail and 2 in ids:
                    raise urllib.error.HTTPError(request.full_url, 402, "quota", {}, io.BytesIO(b"{}"))
                return Response(envelope("kimi", json.dumps({"items": [{"id": i, "text": "早上好"} for i in ids]})))

            provider = create_api_provider("kimi", "secret", model="explicit-model", opener=opener)
            args = (source, root / "zh.srt", root / "report.json", root / "cache", "normal", provider, provider.default_model)
            options = dict(batch_size=1, concurrency=1, retries=3, use_global_context=False, polish=False)
            with self.assertRaises(FatalProviderError):
                run_translation(*args, **options)
            self.assertFalse((root / "zh.srt").exists())
            self.assertEqual([1, 2], called_ids)
            fail = False
            run_translation(*args, **options)
            self.assertEqual([1, 2, 2, 3], called_ids)
            self.assertEqual(3, len(parse_srt(root / "zh.srt")))


class ApiCliTests(unittest.TestCase):
    base_args = ["--input", "in.srt", "--output", "out.srt"]

    def test_cli_dispatches_explicit_provider_model_endpoint_without_network(self):
        for name in API_PROVIDERS:
            with self.subTest(provider=name), mock.patch.dict(os.environ, {"SUBTITLE_API_KEY": "only-in-process", "SUBTITLE_MODEL": "old-environment-model"}), mock.patch(
                "translation_flow.translation.cli.create_api_provider"
            ) as factory, mock.patch("translation_flow.translation.cli.run_translation", return_value={"warnings": []}) as run:
                args = self.base_args + ["--provider", name, "--model", DEFAULT_DEEPSEEK_MODEL if name == "deepseek" else "explicit-model", "--consent-subtitle-text-to-provider", name]
                if name == "qwen":
                    args += ["--endpoint-profile", "qwen-intl", "--max-output-tokens", "16000"]
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(0, cli_main(args))
                self.assertEqual(name, factory.call_args.args[0])
                self.assertEqual("only-in-process", factory.call_args.args[1])
                self.assertEqual(240, factory.call_args.kwargs["timeout"])
                self.assertNotIn("SUBTITLE_API_KEY", os.environ)
                self.assertIs(factory.return_value, run.call_args.args[5].wrapped)
                if name == "qwen":
                    self.assertEqual("qwen-intl", factory.call_args.kwargs["endpoint"])
                    self.assertEqual(16000, factory.call_args.kwargs["max_output_tokens"])

    def test_cli_rejects_cross_provider_consent_and_never_constructs_provider(self):
        cases = [
            ["--provider", "openai", "--model", "explicit-model", "--consent-subtitle-text-to-deepseek"],
            ["--provider", "qwen", "--model", "explicit-model", "--consent-subtitle-text-to-provider", "kimi"],
            ["--provider", "gemini", "--model", "explicit-model"],
            ["--provider", "openai", "--consent-subtitle-text-to-provider", "openai"],
            ["--provider", "anthropic", "--model", "explicit-model", "--endpoint-profile", "qwen-cn", "--consent-subtitle-text-to-provider", "anthropic"],
        ]
        for extra in cases:
            with self.subTest(args=extra), mock.patch.dict(os.environ, {"SUBTITLE_API_KEY": "only-in-process", "SUBTITLE_MODEL": "ignored-for-other-provider"}), mock.patch(
                "translation_flow.translation.cli.create_api_provider"
            ) as factory, mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                try:
                    cli_main(self.base_args + extra)
                finally:
                    factory.assert_not_called()

    def test_old_deepseek_invocation_and_identity_remain_supported(self):
        with mock.patch.dict(os.environ, {"SUBTITLE_API_KEY": "only-in-process"}), mock.patch("translation_flow.translation.cli.create_api_provider", wraps=create_api_provider) as factory, mock.patch(
            "translation_flow.translation.cli.run_translation", return_value={"warnings": []}
        ) as run, mock.patch("sys.stdout", new_callable=io.StringIO):
            os.environ.pop("SUBTITLE_MODEL", None)
            cli_main(self.base_args + ["--consent-subtitle-text-to-deepseek"])
            self.assertEqual("deepseek", factory.call_args.args[0])
            self.assertEqual(DEFAULT_DEEPSEEK_MODEL, run.call_args.args[6])
            self.assertEqual("deepseek:https://api.deepseek.com/chat/completions", run.call_args.args[5].cache_identity)


if __name__ == "__main__":
    unittest.main()
