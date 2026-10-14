from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .output import OutputTarget


SCHEMA_VERSION = 1
WORKFLOW_MODE = "media_repackage"
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_SAFE_STEM = re.compile(r"[^\w.-]+", flags=re.UNICODE)


def _safe_stem(value: str) -> str:
    normalized = _SAFE_STEM.sub("-", value).strip("-._")
    return normalized[:100] or "media"


@dataclass(frozen=True)
class MediaRepackageRequest:
    schema_version: int
    job_id: str
    source_media: Path
    output_dir: Path
    output_target: OutputTarget

    @property
    def job_root(self) -> Path:
        return (self.output_dir / ".media-repackage" / self.job_id).resolve()

    @property
    def output_path(self) -> Path:
        stem = _safe_stem(self.source_media.stem)
        return (self.output_dir / f"{stem}.zho-softsub{self.extension}").resolve()

    @property
    def extension(self) -> str:
        return ".mkv" if self.output_target is OutputTarget.MKV else ".mp4"

    @property
    def technical_report_path(self) -> Path:
        return self.job_root / "technical-validation.report.json"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MediaRepackageRequest":
        schema_version = int(value.get("schema_version", 0))
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"不支持的媒体任务协议版本：{schema_version}")
        if value.get("workflow_mode") != WORKFLOW_MODE:
            raise ValueError(f"不支持的媒体任务模式：{value.get('workflow_mode')!r}")
        for forbidden in ("provider", "consent", "profile_id"):
            if forbidden in value:
                raise ValueError(f"媒体重新封装请求不应包含 {forbidden}")
        job_id = str(value.get("job_id", "")).strip()
        if not _JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
            raise ValueError("job_id 必须是单个安全的任务名称")
        source_text = str(value.get("source_media", "")).strip()
        output_text = str(value.get("output_dir", "")).strip()
        if not source_text:
            raise ValueError("源媒体不能为空")
        if not output_text:
            raise ValueError("输出目录不能为空")
        source_media = Path(source_text).expanduser()
        if not source_media.is_file():
            raise FileNotFoundError(f"源媒体不存在：{source_media}")
        try:
            output_target = OutputTarget(str(value.get("output_target", "")))
        except ValueError as exc:
            raise ValueError("输出容器只能是 mkv 或 mp4") from exc
        return cls(
            schema_version=schema_version,
            job_id=job_id,
            source_media=source_media.resolve(),
            output_dir=Path(output_text).expanduser().resolve(),
            output_target=output_target,
        )
