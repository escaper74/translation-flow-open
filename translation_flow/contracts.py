from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
SUPPORTED_PROFILES = {"normal", "adult_direct"}
SUPPORTED_ASR_A_MODELS = {"large-v3", "large-v3-turbo"}
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
SUPPORTED_DEEPSEEK_MODELS = {"deepseek-v4-pro", "deepseek-v4-flash"}
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@dataclass(frozen=True)
class ProviderRequest:
    id: str = "deepseek"
    host: str = "api.deepseek.com"
    model: str = DEFAULT_DEEPSEEK_MODEL

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderRequest":
        if not isinstance(value, Mapping):
            raise ValueError("provider 必须是 JSON object")
        from .translation.api_providers import API_PROVIDERS, DEFAULT_ENDPOINTS, resolve_endpoint, resolve_model
        from urllib.parse import urlsplit

        provider_id = value.get("id", "")
        if not isinstance(provider_id, str):
            raise ValueError("provider.id 必须是字符串")
        if provider_id in API_PROVIDERS and provider_id != "deepseek":
            if set(value) - {"id", "host", "model", "endpoint_profile", "max_output_tokens"}:
                raise ValueError("provider 只能包含公开配置；API Key 仅从本次环境传入")
            endpoint = value.get("endpoint_profile", DEFAULT_ENDPOINTS[provider_id])
            if not isinstance(endpoint, str) or not endpoint:
                raise ValueError("endpoint_profile 必须是命名端点")
            selected = resolve_endpoint(provider_id, endpoint)
            host = urlsplit(selected.url).netloc
            if value.get("host", host) != host:
                raise ValueError("provider.host 与所选命名端点不匹配")
            cap = value.get("max_output_tokens", 8192)
            if type(cap) is not int or cap <= 0:
                raise ValueError("max_output_tokens 必须是正整数")
            model = value.get("model")
            if not isinstance(model, str):
                raise ValueError("必须明确指定模型 ID")
            return ApiProviderRequest(provider_id, host, resolve_model(provider_id, model), endpoint, cap)
        provider = cls(
            id=str(value.get("id", "")),
            host=str(value.get("host", "")),
            model=str(value.get("model", "")),
        )
        if provider.id == "codex":
            if provider.host != "chatgpt.com" or provider.model != "cli-default":
                raise ValueError("Codex 只支持本机 ChatGPT 登录与 cli-default 模型策略")
            return provider
        if provider.id != "deepseek" or provider.host != "api.deepseek.com":
            raise ValueError("不支持的翻译供应商")
        if provider.model not in SUPPORTED_DEEPSEEK_MODELS:
            raise ValueError(
                "首版只支持仍在服务的 DeepSeek V4 模型："
                + ", ".join(sorted(SUPPORTED_DEEPSEEK_MODELS))
            )
        if value.get("endpoint_profile", "deepseek") != "deepseek" or value.get("max_output_tokens") is not None:
            raise ValueError("DeepSeek 保留既有端点与输出上限，不接受自定义覆盖")
        return provider


@dataclass(frozen=True)
class ApiProviderRequest(ProviderRequest):
    # Keep the exact three-field serialization of old DeepSeek/Codex requests.
    endpoint_profile: str = ""
    max_output_tokens: int = 8192

    @property
    def cache_identity(self) -> str:
        from .translation.api_providers import api_cache_identity
        return api_cache_identity(self.id, self.endpoint_profile, self.max_output_tokens)


@dataclass(frozen=True)
class RunConsent:
    current_run: bool
    subtitle_text_only: bool
    audio_video_local: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunConsent":
        consent = cls(
            current_run=value.get("current_run") is True,
            subtitle_text_only=value.get("subtitle_text_only") is True,
            audio_video_local=value.get("audio_video_local") is True,
        )
        if not all(
            (consent.current_run, consent.subtitle_text_only, consent.audio_video_local)
        ):
            raise ValueError(
                "本次尚未确认仅把字幕文本发送至所选翻译服务，音视频留在本机"
            )
        return consent


@dataclass(frozen=True)
class JobRequest:
    schema_version: int
    job_id: str
    input_video: Path
    output_dir: Path
    profile_id: str
    provider: ProviderRequest
    consent: RunConsent
    additional_mp4: bool = False
    asr_a_model: str = "large-v3"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "JobRequest":
        schema_version = int(value.get("schema_version", 0))
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"不支持的任务协议版本：{schema_version}")
        job_id = str(value.get("job_id", "")).strip()
        if not _JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
            raise ValueError("job_id 必须是单个安全的任务名称")
        profile_id = str(value.get("profile_id", "")).strip()
        if profile_id not in SUPPORTED_PROFILES:
            raise ValueError(f"不支持的翻译模式：{profile_id}")
        input_text = str(value.get("input_video", "")).strip()
        output_text = str(value.get("output_dir", "")).strip()
        if not input_text:
            raise ValueError("输入影片不能为空")
        if not output_text:
            raise ValueError("输出目录不能为空")
        input_video = Path(input_text).expanduser()
        output_dir = Path(output_text).expanduser()
        if not input_video.is_file():
            raise FileNotFoundError(f"输入影片不存在：{input_video}")
        delivery = value.get("delivery", {})
        if delivery is None:
            delivery = {}
        if not isinstance(delivery, Mapping) or type(delivery.get("additional_mp4", False)) is not bool:
            raise ValueError("delivery.additional_mp4 必须是 boolean")
        provider = ProviderRequest.from_mapping(value.get("provider", {}))
        asr_a_model = value.get("asr_a_model", "large-v3")
        if not isinstance(asr_a_model, str) or asr_a_model not in SUPPORTED_ASR_A_MODELS:
            raise ValueError("asr_a_model 必须是 large-v3 或 large-v3-turbo")
        consent = value.get("consent", {})
        if not isinstance(consent, Mapping):
            raise ValueError("consent 必须是 JSON object")
        if isinstance(provider, ApiProviderRequest) and consent.get("provider") != provider.id:
            raise ValueError("consent.provider 必须明确匹配本次所选翻译服务")
        return cls(
            schema_version=schema_version,
            job_id=job_id,
            input_video=input_video.resolve(),
            output_dir=output_dir.resolve(),
            profile_id=profile_id,
            provider=provider,
            consent=RunConsent.from_mapping(consent),
            additional_mp4=delivery.get("additional_mp4", False),
            asr_a_model=asr_a_model,
        )
