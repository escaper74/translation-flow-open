from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PIPELINE_VERSION, STATE_SCHEMA_VERSION


class ResumeMismatchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file(path: Path) -> dict[str, Any]:
    stat_result = path.stat()
    return {
        "path": str(path.resolve()),
        "bytes": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def fingerprint_matches(fingerprint: dict[str, Any] | None) -> bool:
    if not fingerprint:
        return False
    path = Path(str(fingerprint.get("path", "")))
    if not path.is_file():
        return False
    stat_result = path.stat()
    if stat_result.st_size != fingerprint.get("bytes"):
        return False
    return sha256_file(path) == fingerprint.get("sha256")


class JobState:
    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def create_or_load(
        cls,
        path: Path,
        *,
        job_id: str,
        cache_key: str,
        request: dict[str, Any],
    ) -> tuple["JobState", bool]:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            state = cls(path, data)
            state.assert_compatible(cache_key, request)
            return state, True
        data = {
            "schema_version": STATE_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "job_id": job_id,
            "cache_key": cache_key,
            "status": "created",
            "created_at_utc": utc_now(),
            "updated_at_utc": utc_now(),
            "request": request,
            "stages": {},
            "attempt_artifacts": [],
            "cleanup": [],
        }
        state = cls(path, data)
        state.save()
        return state, False

    def assert_compatible(self, cache_key: str, request: dict[str, Any]) -> None:
        if self.data.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ResumeMismatchError("任务记录版本不同，拒绝复用旧任务目录")
        if self.data.get("pipeline_version") != PIPELINE_VERSION:
            raise ResumeMismatchError("流水线版本不同，拒绝复用旧任务目录")
        if self.data.get("cache_key") != cache_key:
            raise ResumeMismatchError(
                "输入、选项、工具/模型或输出路径已变化，拒绝错误复用旧缓存"
            )
        if self.data.get("request") != request:
            raise ResumeMismatchError("任务请求记录与本次请求不同，拒绝恢复")

    def save(self) -> None:
        self.data["updated_at_utc"] = utc_now()
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temp, self.path)

    def set_status(self, status: str, **fields: Any) -> None:
        self.data["status"] = status
        self.data.update(fields)
        self.save()

    def stage(self, name: str) -> dict[str, Any]:
        return self.data.setdefault("stages", {}).setdefault(name, {})

    def start_stage(self, name: str, **fields: Any) -> None:
        stage = self.stage(name)
        stage.update(
            {
                "status": "running",
                "started_at_utc": utc_now(),
                "completed_at_utc": None,
                **fields,
            }
        )
        self.save()

    def complete_stage(self, name: str, **fields: Any) -> None:
        stage = self.stage(name)
        stage.update(
            {
                "status": "completed",
                "completed_at_utc": utc_now(),
                **fields,
            }
        )
        self.save()

    def fail_stage(self, name: str, message: str) -> None:
        stage = self.stage(name)
        stage.update(
            {"status": "failed", "failed_at_utc": utc_now(), "error": message}
        )
        self.save()

    def reusable_artifact(self, stage_name: str) -> Path | None:
        stage = self.data.get("stages", {}).get(stage_name, {})
        if stage.get("status") != "completed":
            return None
        fingerprint = stage.get("artifact")
        if not fingerprint_matches(fingerprint):
            return None
        return Path(fingerprint["path"])

    def record_attempt(self, stage: str, path: Path, status: str) -> None:
        self.data.setdefault("attempt_artifacts", []).append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "status": status,
                "recorded_at_utc": utc_now(),
            }
        )
        self.save()

    def record_cleanup(self, path: Path, reason: str) -> None:
        self.data.setdefault("cleanup", []).append(
            {
                "path": str(path.resolve()),
                "reason": reason,
                "completed_at_utc": utc_now(),
            }
        )
        self.save()
