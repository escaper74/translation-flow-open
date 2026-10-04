"""Standalone Japanese SRT translation through a text API or local Codex login."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

from translation_flow.contracts import DEFAULT_DEEPSEEK_MODEL
from translation_flow.cancellation import CancellableProvider, interrupts

from .api_providers import API_PROVIDERS, ENDPOINTS, create_api_provider, resolve_endpoint, resolve_model
from .codex_provider import CodexProvider, DEFAULT_MODEL as CODEX_MODEL, TranslationCancelled
from .pipeline import run_translation
from .profiles import TranslationProfile


TRANSLATION_PROVIDERS = (*API_PROVIDERS, "codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate Japanese SRT to Simplified Chinese through six text APIs or Codex."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--provider", choices=TRANSLATION_PROVIDERS, default="deepseek")
    parser.add_argument("--model", help="Explicit API model ID; DeepSeek retains its default and Codex uses cli-default.")
    parser.add_argument("--endpoint-profile", choices=tuple(ENDPOINTS), help="Named official endpoint; Qwen/Kimi default to China.")
    parser.add_argument("--max-output-tokens", type=int, help="New API output cap (default 8192); omit for legacy DeepSeek.")
    parser.add_argument(
        "--consent-subtitle-text-to-provider", choices=TRANSLATION_PROVIDERS,
        help="Authorize this run to send subtitle-derived text to exactly this provider.",
    )
    parser.add_argument(
        "--consent-subtitle-text-to-deepseek",
        action="store_true",
        help="Confirm this run may send subtitle-derived text to api.deepseek.com.",
    )
    parser.add_argument("--profile", choices=("normal", "adult_direct"))
    parser.add_argument("--batch-size", type=int, default=60)
    parser.add_argument("--max-chars", type=int, default=5000)
    parser.add_argument("--temperature", type=float, default=0.25, help="Legacy DeepSeek setting; new APIs use model-default sampling.")
    parser.add_argument("--timeout", type=int, help="Seconds per call: API default 240, Codex default 300.")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--context-chars", type=int, default=18000)
    parser.add_argument("--style-instruction", default="")
    parser.add_argument("--no-global-context", action="store_true")
    parser.add_argument("--skip-polish", action="store_true")
    parser.add_argument("--force", action="store_true", help="Recompute batches; never overwrite final SRT/report files.")
    return parser


def main(argv: list[str] | None = None, *, progress_callback=None, cancelled=None,
         log=None) -> int:
    log = log or print
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.profile and args.style_instruction.strip():
        parser.error("--profile and --style-instruction cannot be used together")
    is_codex = args.provider == "codex"
    timeout = args.timeout if args.timeout is not None else (300 if is_codex else 240)
    if timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        if is_codex:
            if args.endpoint_profile is not None or args.max_output_tokens is not None:
                parser.error("Codex uses the local ChatGPT login; API endpoint/token-cap options do not apply.")
            selected_model = args.model if args.model is not None else CODEX_MODEL
            if selected_model != CODEX_MODEL:
                parser.error("Codex --model must be cli-default (or omitted).")
            destination = "chatgpt.com (official Codex CLI, local ChatGPT login)"
        else:
            selected_endpoint = resolve_endpoint(args.provider, args.endpoint_profile)
            selected_model = args.model
            if selected_model is None and args.provider == "deepseek":
                selected_model = os.environ.get("SUBTITLE_MODEL", DEFAULT_DEEPSEEK_MODEL)
            selected_model = resolve_model(args.provider, selected_model)
            destination = selected_endpoint.url
    except ValueError as exc:
        parser.error(str(exc))
    if args.consent_subtitle_text_to_provider not in (None, args.provider):
        parser.error("Consent provider does not match --provider; no request sent.")
    if args.provider != "deepseek" and args.consent_subtitle_text_to_deepseek:
        parser.error("DeepSeek consent cannot authorize a different provider.")
    has_consent = args.consent_subtitle_text_to_provider == args.provider or (
        args.provider == "deepseek" and args.consent_subtitle_text_to_deepseek
    )
    if not has_consent:
        parser.error(f"Missing current-run subtitle-text consent for {args.provider} ({destination}).")
    api_key = ""
    if not is_codex:
        api_key = os.environ.pop("SUBTITLE_API_KEY", "")
        if not api_key:
            parser.error("Missing API key. Set SUBTITLE_API_KEY for this process.")

    profile_id = args.profile or "normal"
    profile_override = None
    if args.style_instruction.strip():
        style = args.style_instruction.strip()
        style_hash = hashlib.sha256(style.encode("utf-8")).hexdigest()[:16]
        profile_id = f"legacy_custom_{style_hash}"
        profile_override = TranslationProfile(profile_id, style)

    cache_root = args.cache_dir or (args.output.parent / "translate_cache")
    with interrupts() as signal_cancelled:
        should_cancel = lambda: signal_cancelled() or (cancelled is not None and cancelled())
        try:
            if is_codex:
                # Keep call totals scoped to this output when jobs share a cache.
                audit_id = hashlib.sha256(str(args.output.resolve()).encode("utf-8")).hexdigest()[:16]
                provider = CodexProvider(
                    audit_dir=cache_root / "codex-calls" / audit_id,
                    model=selected_model, timeout=timeout, cancelled=should_cancel,
                )
            else:
                provider = create_api_provider(
                    args.provider, api_key, model=selected_model,
                    endpoint=args.endpoint_profile, max_output_tokens=args.max_output_tokens,
                    timeout=timeout,
                )
                provider = CancellableProvider(provider, should_cancel)
        except ValueError as exc:
            parser.error(str(exc))
        log(f"[provider] {args.provider} / {selected_model} / {destination}")
        try:
            report = run_translation(
                args.input,
                args.output,
                args.report,
                cache_root,
                profile_id,
                provider,
                selected_model,
                progress_callback=progress_callback,
                batch_size=args.batch_size,
                max_chars=args.max_chars,
                context_chars=args.context_chars,
                temperature=args.temperature,
                retries=args.retries,
                concurrency=args.concurrency,
                use_global_context=not args.no_global_context,
                polish=not args.skip_polish,
                force=args.force,
                profile_override=profile_override,
            )
        except TranslationCancelled as exc:
            log(f"[cancelled] {exc}")
            return 2
    for warning in report["warnings"]:
        log(f"[warn] {warning}")
    log(f"[done] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
