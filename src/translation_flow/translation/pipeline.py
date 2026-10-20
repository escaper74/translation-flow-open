"""One production translation pipeline shared by every entry point."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from translation_flow.srt import (
    SubtitleCue,
    parse_srt,
    subtitle_digest,
    validate_timeline,
    write_srt,
)

from .profiles import TranslationProfile, get_profile
from .provider import FatalProviderError, TranslationProvider


KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff65-\uff9f]")
ProgressCallback = Callable[[dict[str, Any]], None]
NEIGHBOR_CUES_PER_SIDE = 8
TRANSLATION_CONTEXT_STRATEGY = "fixed_source_neighbors_and_initial_snapshot_v2"


class TranslationPipelineError(RuntimeError):
    pass


class TranslationValidationError(TranslationPipelineError):
    pass


class ProviderAttemptsExhausted(TranslationPipelineError):
    pass


def _emit(callback: ProgressCallback | None, event: str, **values: Any) -> None:
    if callback is not None:
        callback({"event": event, **values})


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\\n", "\n")).strip()


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{uuid.uuid4().hex[:12]}.tmp"
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("provider response must be a JSON object")
    return value


def parse_item_response(
    content: str,
    expected_ids: Sequence[int],
    *,
    text_keys: Sequence[str] = ("text",),
) -> dict[int, str]:
    payload = _extract_json_object(content)
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("provider response must contain an items array")
    if not text_keys:
        raise ValueError("at least one provider text key is required")
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            keys = sorted(item) if isinstance(item, dict) else []
            raise ValueError(f"each provider item must contain id; keys={keys}")
        item_id = int(item["id"])
        if item_id in result:
            raise ValueError(f"duplicate provider item ID: {item_id}")
        text_key = next((key for key in text_keys if key in item), None)
        if text_key is None:
            raise ValueError(
                f"provider item at ID {item_id} lacks an accepted text field; "
                f"keys={sorted(item)} expected={list(text_keys)}"
            )
        if not isinstance(item[text_key], str):
            raise ValueError(f"provider {text_key} at ID {item_id} is not a string")
        result[item_id] = _clean_text(item[text_key])
    if set(result) != set(expected_ids):
        raise ValueError(
            f"provider ID mismatch: missing={sorted(set(expected_ids)-set(result))} "
            f"extra={sorted(set(result)-set(expected_ids))}"
        )
    return result


def cache_namespace(
    cache_root: str | Path,
    *,
    profile: TranslationProfile,
    provider: TranslationProvider,
    model: str,
    source_digest: str,
    pipeline_options: dict[str, Any],
) -> Path:
    identity = json.dumps(
        {
            "profile_id": profile.profile_id,
            "style_instruction": profile.style_instruction,
            "provider_id": provider.provider_id,
            "provider_identity": provider.cache_identity,
            "model": model,
            "source_sha256": source_digest,
            "pipeline_options": pipeline_options,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # One full SHA-256 segment keeps Windows paths below MAX_PATH while still
    # isolating every behavior-bearing identity above. Legacy flat cache files
    # are outside this v1 namespace and are never inspected.
    namespace_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(cache_root) / "v1" / namespace_digest


def make_batches(
    cues: Sequence[SubtitleCue], batch_size: int, max_chars: int
) -> list[list[SubtitleCue]]:
    if batch_size <= 0 or max_chars <= 0:
        raise ValueError("batch_size and max_chars must be positive")
    batches: list[list[SubtitleCue]] = []
    current: list[SubtitleCue] = []
    used = 0
    for cue in cues:
        size = len(cue.text) + 32
        if current and (len(current) >= batch_size or used + size > max_chars):
            batches.append(current)
            current, used = [], 0
        current.append(cue)
        used += size
    if current:
        batches.append(current)
    return batches


def context_chunks(cues: Sequence[SubtitleCue], max_chars: int) -> list[list[SubtitleCue]]:
    # Unlike the legacy stride sampler, every cue is assigned to exactly one chunk.
    return make_batches(cues, max(len(cues), 1), max_chars)


def _check_cancelled(provider: TranslationProvider) -> None:
    check = getattr(provider, "check_cancelled", None)
    if callable(check):
        check()


def _chat_with_retries(
    provider: TranslationProvider,
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
    retries: int,
    json_output: bool = False,
) -> str:
    if retries <= 0:
        raise ValueError("retries must be positive")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        _check_cancelled(provider)
        try:
            return provider.chat(
                messages,
                model=model,
                temperature=temperature,
                json_output=json_output,
            )
        except FatalProviderError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                delay = min(2**attempt, 8)
                retry_after = getattr(exc, "retry_after_seconds", None)
                if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and 0 <= retry_after <= 300:
                    delay = max(delay, retry_after)
                wait = getattr(provider, "wait_retry", None)
                if callable(wait):
                    wait(delay)
                else:
                    time.sleep(delay)
    raise ProviderAttemptsExhausted(
        f"provider call failed after {retries} attempts: {last_error}"
    )


def _context_messages(
    cues: Sequence[SubtitleCue], profile: TranslationProfile
) -> list[dict[str, str]]:
    source = "\n".join(f"{cue.index}: {cue.text}" for cue in cues)
    return [
        {
            "role": "system",
            "content": (
                "为日语到简体中文字幕制作语境表。提取人物、关系、称谓、专名、语气、场景和易错词。"
                "不要逐句翻译，输出紧凑的简体中文。"
            ),
        },
        {
            "role": "user",
            "content": f"翻译风格：{profile.style_instruction}\n\n本段完整字幕：\n{source}",
        },
    ]


def _merge_context_messages(
    summaries: Sequence[str], profile: TranslationProfile
) -> list[dict[str, str]]:
    joined = "\n\n".join(
        f"分段语境 {index}:\n{summary}" for index, summary in enumerate(summaries, 1)
    )
    return [
        {
            "role": "system",
            "content": "合并分段语境表，消除重复并统一人物称呼和术语。输出紧凑的简体中文。",
        },
        {"role": "user", "content": f"翻译风格：{profile.style_instruction}\n\n{joined}"},
    ]


def build_full_context(
    cues: Sequence[SubtitleCue],
    profile: TranslationProfile,
    provider: TranslationProvider,
    *,
    model: str,
    cache_dir: Path,
    context_chars: int,
    retries: int,
    force: bool,
    progress_callback: ProgressCallback | None,
) -> str:
    _check_cancelled(provider)
    final_path = cache_dir / "context" / "global.txt"
    if final_path.exists() and not force:
        cached_context = final_path.read_text(encoding="utf-8").strip()
        if not cached_context:
            raise TranslationPipelineError(f"empty context cache: {final_path}")
        return cached_context
    chunks = context_chunks(cues, context_chars)
    summaries: list[str] = []
    for number, chunk in enumerate(chunks, start=1):
        _check_cancelled(provider)
        chunk_path = cache_dir / "context" / f"chunk_{number:04d}.txt"
        if chunk_path.exists() and not force:
            summary = chunk_path.read_text(encoding="utf-8").strip()
            if not summary:
                raise TranslationPipelineError(f"empty context cache: {chunk_path}")
        else:
            summary = _chat_with_retries(
                provider,
                _context_messages(chunk, profile),
                model=model,
                temperature=0.1,
                retries=retries,
            ).strip()
            if not summary:
                raise TranslationPipelineError(f"context chunk {number} returned empty text")
            _write_text_atomic(chunk_path, summary)
        summaries.append(summary)
        _emit(progress_callback, "context_chunk_completed", current=number, total=len(chunks))
    context = summaries[0] if len(summaries) == 1 else _chat_with_retries(
        provider,
        _merge_context_messages(summaries, profile),
        model=model,
        temperature=0.1,
        retries=retries,
    ).strip()
    if not context:
        raise TranslationPipelineError("global context returned empty text")
    _write_text_atomic(final_path, context)
    return context


def _translation_messages(
    batch: Sequence[SubtitleCue],
    profile: TranslationProfile,
    global_context: str,
    neighbors: Sequence[SubtitleCue],
) -> list[dict[str, str]]:
    neighbor_items = [{"id": cue.index, "text": cue.text} for cue in neighbors]
    items = [{"id": cue.index, "text": cue.text} for cue in batch]
    return [
        {
            "role": "system",
            "content": (
                "你是日语到简体中文字幕译者。忠实、自然、简洁，不解释、不删减、不说教。"
                "只返回合法 JSON：{\"items\":[{\"id\":1,\"text\":\"...\"}]}。"
                f"翻译风格：{profile.style_instruction}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"全片语境：\n{global_context or '(无)'}\n\n"
                "相邻日文只供理解人物、语气和前后关系，不要翻译或返回这些相邻 ID：\n"
                f"{json.dumps({'items': neighbor_items}, ensure_ascii=False) if neighbor_items else '(无)'}\n\n"
                f"待翻译：\n{json.dumps({'items': items}, ensure_ascii=False)}"
            ),
        },
    ]


def _polish_messages(
    batch: Sequence[SubtitleCue],
    translated: dict[int, str],
    profile: TranslationProfile,
    global_context: str,
    neighbors: Sequence[SubtitleCue],
) -> list[dict[str, str]]:
    items = [
        {"id": cue.index, "ja": cue.text, "zh": translated[cue.index]}
        for cue in batch
    ]
    neighbor_items = [
        {"id": cue.index, "ja": cue.text, "zh": translated[cue.index]}
        for cue in neighbors
    ]
    return [
        {
            "role": "system",
            "content": (
                "对照日文润色简体中文字幕，修正错译、直译腔、称呼不一致和过长表达。"
                "不解释，只返回合法 JSON：{\"items\":[{\"id\":1,\"text\":\"...\"}]}。"
                "每项只能用原 ID 和 text 字段承载润色后的中文，不要返回 ja、zh、polished 等其他字段名。"
                f"翻译风格：{profile.style_instruction}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"全片语境：\n{global_context or '(无)'}\n\n"
                "相邻初译只供检查称呼、语气和衔接，不要返回这些相邻 ID：\n"
                f"{json.dumps({'items': neighbor_items}, ensure_ascii=False) if neighbor_items else '(无)'}\n\n"
                f"待润色：\n{json.dumps({'items': items}, ensure_ascii=False)}"
            ),
        },
    ]


def _neighbor_cues(
    all_cues: Sequence[SubtitleCue],
    batch: Sequence[SubtitleCue],
    *,
    per_side: int = NEIGHBOR_CUES_PER_SIDE,
) -> list[SubtitleCue]:
    if not batch or per_side <= 0:
        return []
    positions = {cue.index: position for position, cue in enumerate(all_cues)}
    try:
        batch_positions = [positions[cue.index] for cue in batch]
    except KeyError as exc:
        raise TranslationPipelineError(
            f"batch cue is outside the source timeline: {exc.args[0]}"
        ) from exc
    first, last = min(batch_positions), max(batch_positions)
    before = list(all_cues[max(0, first - per_side) : first])
    after = list(all_cues[last + 1 : last + 1 + per_side])
    return before + after


def _repair_messages(
    batch: Sequence[SubtitleCue],
    translated: dict[int, str],
    all_cues: Sequence[SubtitleCue],
    profile: TranslationProfile,
    global_context: str,
) -> list[dict[str, str]]:
    positions = {cue.index: pos for pos, cue in enumerate(all_cues)}
    items = []
    for cue in batch:
        pos = positions[cue.index]
        items.append(
            {
                "id": cue.index,
                "previous_ja": all_cues[pos - 1].text if pos else "",
                "ja": cue.text,
                "current_zh": translated[cue.index],
                "next_ja": all_cues[pos + 1].text if pos + 1 < len(all_cues) else "",
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "定点修复仍含日文假名的中文字幕。邻句只供理解，只输出目标 ID 的纯简体中文。"
                "只返回合法 JSON：{\"items\":[{\"id\":1,\"text\":\"...\"}]}。"
                "每项只能用原 ID 和 text 字段承载修复后的中文，不要返回 ja、current_zh、repaired 等其他字段名。"
                f"翻译风格：{profile.style_instruction}"
            ),
        },
        {
            "role": "user",
            "content": f"全片语境：\n{global_context or '(无)'}\n\n{json.dumps({'items': items}, ensure_ascii=False)}",
        },
    ]


def _cache_path(cache_dir: Path, phase: str, batch: Sequence[SubtitleCue]) -> Path:
    ids = ",".join(str(cue.index) for cue in batch)
    suffix = hashlib.sha256(ids.encode("ascii")).hexdigest()[:10]
    return cache_dir / "batches" / phase / f"{batch[0].index:06d}_{batch[-1].index:06d}_{suffix}.json"


def _load_cached_cover(
    cache_dir: Path, phase: str, expected_ids: Sequence[int]
) -> dict[int, str]:
    found: dict[int, str] = {}
    folder = cache_dir / "batches" / phase
    if not folder.exists():
        return found
    expected = set(expected_ids)
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise TranslationPipelineError(f"invalid batch cache: {path}")
        for item in items:
            item_id = int(item["id"])
            if item_id in expected:
                if not isinstance(item.get("text"), str):
                    raise TranslationPipelineError(f"invalid cached text at ID {item_id}: {path}")
                text = _clean_text(item["text"])
                if not text:
                    raise TranslationPipelineError(f"empty cached text at ID {item_id}: {path}")
                if item_id in found and found[item_id] != text:
                    raise TranslationPipelineError(f"conflicting cached result at ID {item_id}")
                found[item_id] = text
    return found


def _persist_batch(
    path: Path, phase: str, result: dict[int, str], *, codex_response: str | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase,
        "items": [
            {"id": item_id, "text": result[item_id]}
            for item_id in sorted(result)
        ],
    }
    if codex_response is not None:
        response = json.loads(codex_response)
        notes = {item["id"]: item.get("note", "") for item in response["items"]}
        for item in payload["items"]:
            item["note"] = notes.get(item["id"], "")
        payload["codex_execution"] = response.get("_codex", {})
    temporary = path.parent / f".{uuid.uuid4().hex[:12]}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _missing_groups(
    batch: Sequence[SubtitleCue], cached: dict[int, str]
) -> list[list[SubtitleCue]]:
    groups: list[list[SubtitleCue]] = []
    current: list[SubtitleCue] = []
    for cue in batch:
        if cue.index in cached:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(cue)
    if current:
        groups.append(current)
    return groups


def _run_batch_resilient(
    batch: Sequence[SubtitleCue],
    *,
    phase: str,
    cache_dir: Path,
    provider: TranslationProvider,
    model: str,
    temperature: float,
    retries: int,
    force: bool,
    messages_factory: Callable[[Sequence[SubtitleCue]], list[dict[str, str]]],
) -> dict[int, str]:
    _check_cancelled(provider)
    expected_ids = [cue.index for cue in batch]
    cached = {} if force else _load_cached_cover(cache_dir, phase, expected_ids)
    if len(cached) == len(expected_ids):
        return {item_id: cached[item_id] for item_id in expected_ids}
    if cached:
        result = dict(cached)
        for group in _missing_groups(batch, cached):
            result.update(
                _run_batch_resilient(
                    group,
                    phase=phase,
                    cache_dir=cache_dir,
                    provider=provider,
                    model=model,
                    temperature=temperature,
                    retries=retries,
                    force=force,
                    messages_factory=messages_factory,
                )
            )
        return {item_id: result[item_id] for item_id in expected_ids}
    try:
        content = _chat_with_retries(
            provider,
            messages_factory(batch),
            model=model,
            temperature=temperature,
            retries=retries,
            json_output=True,
        )
        phase_text_keys: tuple[str, ...]
        if phase == "polish":
            # V4 may mirror the input field name when a prompt is underspecified.
            # Prefer the canonical `text` contract, then accept a small set of
            # unambiguous transformed aliases from already-issued responses.
            phase_text_keys = (
                "text",
                "polished_text",
                "polished_zh",
                "polished",
                "translation",
                "translated_text",
                "zh",
            )
        elif phase == "kana_repair":
            phase_text_keys = (
                "text",
                "repaired_text",
                "repaired_zh",
                "repaired",
                "translation",
                "translated_text",
                "zh",
            )
        else:
            phase_text_keys = ("text",)
        result = parse_item_response(content, expected_ids, text_keys=phase_text_keys)
        if any(not text for text in result.values()):
            raise ValueError("provider returned empty subtitle text")
    except (FatalProviderError, ProviderAttemptsExhausted):
        raise
    except Exception as exc:
        if len(batch) <= 1:
            if isinstance(exc, TranslationPipelineError):
                raise
            raise TranslationPipelineError(
                f"{phase} cue #{batch[0].index} failed: {exc}"
            ) from exc
        midpoint = len(batch) // 2
        left = _run_batch_resilient(
            batch[:midpoint],
            phase=phase,
            cache_dir=cache_dir,
            provider=provider,
            model=model,
            temperature=temperature,
            retries=retries,
            force=force,
            messages_factory=messages_factory,
        )
        right = _run_batch_resilient(
            batch[midpoint:],
            phase=phase,
            cache_dir=cache_dir,
            provider=provider,
            model=model,
            temperature=temperature,
            retries=retries,
            force=force,
            messages_factory=messages_factory,
        )
        left.update(right)
        return left
    cache_path = _cache_path(cache_dir, phase, batch)
    try:
        if provider.provider_id == "codex":
            _persist_batch(cache_path, phase, result, codex_response=content)
        else:
            _persist_batch(cache_path, phase, result)
    except OSError as exc:
        raise TranslationPipelineError(
            f"validated {phase} batch could not be cached: {cache_path}: {exc}"
        ) from exc
    return result


def _batch_weight(batch: Sequence[SubtitleCue]) -> int:
    return sum(len(cue.text) + 32 for cue in batch)


def split_fixed_contiguous_lanes(
    batches: Sequence[Sequence[SubtitleCue]], concurrency: int
) -> list[list[tuple[int, Sequence[SubtitleCue]]]]:
    """Assign fixed chronological ranges; workers never take from another range."""

    if concurrency not in (1, 2):
        raise ValueError("translation concurrency currently supports only 1 or 2")
    indexed = [(number, batch) for number, batch in enumerate(batches, start=1)]
    if concurrency == 1 or len(indexed) <= 1:
        return [indexed]

    weights = [_batch_weight(batch) for _, batch in indexed]
    total = sum(weights)
    running = 0
    best_split = 1
    best_difference: int | None = None
    for split in range(1, len(indexed)):
        running += weights[split - 1]
        difference = abs(total - 2 * running)
        if best_difference is None or difference < best_difference:
            best_split = split
            best_difference = difference
    return [indexed[:best_split], indexed[best_split:]]


def _validate_phase_map(
    source: Sequence[SubtitleCue], result: dict[int, str], phase: str
) -> None:
    expected = {cue.index for cue in source}
    actual = set(result)
    if actual != expected:
        raise TranslationValidationError(
            f"{phase} ID mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    empty = [item_id for item_id in sorted(result) if not result[item_id].strip()]
    if empty:
        raise TranslationValidationError(f"{phase} contains empty text: {empty}")


def _run_fixed_lane_phase(
    batches: Sequence[Sequence[SubtitleCue]],
    *,
    concurrency: int,
    phase: str,
    start_event: str,
    progress_event: str,
    progress_callback: ProgressCallback | None,
    run_batch: Callable[[Sequence[SubtitleCue]], dict[int, str]],
) -> dict[int, str]:
    lanes = split_fixed_contiguous_lanes(batches, concurrency)
    completed = 0
    progress_lock = threading.Lock()
    stop_requested = threading.Event()

    def run_lane(
        lane_number: int,
        lane: Sequence[tuple[int, Sequence[SubtitleCue]]],
    ) -> dict[int, str]:
        nonlocal completed
        lane_result: dict[int, str] = {}
        try:
            for batch_number, batch in lane:
                if stop_requested.is_set():
                    break
                with progress_lock:
                    _emit(
                        progress_callback,
                        start_event,
                        total=len(batches),
                        batch_number=batch_number,
                        lane=lane_number,
                    )
                batch_started = time.monotonic()
                batch_result = run_batch(batch)
                elapsed_seconds = round(time.monotonic() - batch_started, 3)
                overlap = set(lane_result).intersection(batch_result)
                if overlap:
                    raise TranslationValidationError(
                        f"{phase} lane returned duplicate IDs: {sorted(overlap)}"
                    )
                lane_result.update(batch_result)
                with progress_lock:
                    completed += 1
                    _emit(
                        progress_callback,
                        progress_event,
                        current=completed,
                        total=len(batches),
                        batch_number=batch_number,
                        lane=lane_number,
                        elapsed_seconds=elapsed_seconds,
                    )
            return lane_result
        except Exception:
            stop_requested.set()
            raise

    if len(lanes) == 1:
        lane_results = [run_lane(1, lanes[0])]
    else:
        with ThreadPoolExecutor(
            max_workers=len(lanes), thread_name_prefix=f"subtitle-{phase}"
        ) as executor:
            futures = [
                executor.submit(run_lane, lane_number, lane)
                for lane_number, lane in enumerate(lanes, start=1)
            ]
            try:
                lane_results = [future.result() for future in futures]
            except Exception:
                stop_requested.set()
                for future in futures:
                    future.cancel()
                raise

    merged: dict[int, str] = {}
    for lane_result in lane_results:
        overlap = set(merged).intersection(lane_result)
        if overlap:
            raise TranslationValidationError(
                f"{phase} lanes returned duplicate IDs: {sorted(overlap)}"
            )
        merged.update(lane_result)
    return {item_id: merged[item_id] for item_id in sorted(merged)}


def _validate_final(backbone: Sequence[SubtitleCue], candidate: Sequence[SubtitleCue]) -> None:
    validate_timeline(list(backbone), list(candidate))
    empty_ids = [cue.index for cue in candidate if not cue.text.strip()]
    if empty_ids:
        raise TranslationValidationError(f"empty translated subtitles: {empty_ids}")

    normalized = [
        re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", cue.text)).strip(
            " \t\r\n.,!?。！？、，"
        )
        for cue in candidate
    ]
    counted = [text for text in normalized if len(text) >= 5]
    counts = Counter(counted)
    if counts:
        top_text, top_count = counts.most_common(1)[0]
        if top_count >= 50 and top_count / max(len(candidate), 1) >= 0.18:
            raise TranslationValidationError(
                "translated subtitles show repetition collapse: "
                f"{top_count}/{len(candidate)} text={top_text!r}"
            )
    current = ""
    current_run = 0
    max_run = 0
    max_text = ""
    for text in normalized:
        eligible = text if len(text) >= 5 else ""
        if eligible and eligible == current:
            current_run += 1
        else:
            if current_run > max_run:
                max_run, max_text = current_run, current
            current = eligible
            current_run = 1 if eligible else 0
    if current_run > max_run:
        max_run, max_text = current_run, current
    if max_run >= 12:
        raise TranslationValidationError(
            "translated subtitles repeat consecutively: "
            f"{max_run} text={max_text!r}"
        )


def run_translation(
    input_srt: str | Path,
    output_srt: str | Path,
    report_path: str | Path | None,
    cache_root: str | Path,
    profile_id: str,
    provider: TranslationProvider,
    model: str,
    progress_callback: ProgressCallback | None = None,
    *,
    batch_size: int = 60,
    max_chars: int = 5000,
    context_chars: int = 18000,
    temperature: float = 0.25,
    retries: int = 4,
    use_global_context: bool = True,
    polish: bool = True,
    concurrency: int = 2,
    force: bool = False,
    profile_override: TranslationProfile | None = None,
) -> dict[str, Any]:
    """Translate and atomically publish only a structurally valid final SRT."""

    output = Path(output_srt)
    target_report = Path(report_path) if report_path is not None else None
    if output.exists():
        raise FileExistsError(f"refusing to overwrite translated SRT: {output}")
    if target_report is not None and target_report.exists():
        raise FileExistsError(f"refusing to overwrite translation report: {target_report}")

    source = parse_srt(input_srt)
    if concurrency not in (1, 2):
        raise ValueError("translation concurrency currently supports only 1 or 2")
    profile = profile_override or get_profile(profile_id)
    digest = subtitle_digest(source)
    cache_dir = cache_namespace(
        cache_root,
        profile=profile,
        provider=provider,
        model=model,
        source_digest=digest,
        pipeline_options={
            "batch_size": batch_size,
            "max_chars": max_chars,
            "context_chars": context_chars,
            "temperature": temperature,
            "use_global_context": use_global_context,
            "polish": polish,
            "context_strategy": TRANSLATION_CONTEXT_STRATEGY,
        },
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    _emit(progress_callback, "translation_started", subtitle_count=len(source))

    phase_wall_seconds: dict[str, float] = {}
    global_context = ""
    context_started = time.monotonic()
    if use_global_context:
        global_context = build_full_context(
            source,
            profile,
            provider,
            model=model,
            cache_dir=cache_dir,
            context_chars=context_chars,
            retries=retries,
            force=force,
            progress_callback=progress_callback,
        )
    phase_wall_seconds["global_context"] = round(
        time.monotonic() - context_started, 3
    )

    batches = make_batches(source, batch_size, max_chars)
    planned_lanes = split_fixed_contiguous_lanes(batches, concurrency)
    lane_plan = [
        {
            "lane": lane_number,
            "first_batch": lane[0][0],
            "last_batch": lane[-1][0],
            "batch_count": len(lane),
            "first_subtitle_id": lane[0][1][0].index,
            "last_subtitle_id": lane[-1][1][-1].index,
            "estimated_request_weight": sum(
                _batch_weight(batch) for _, batch in lane
            ),
        }
        for lane_number, lane in enumerate(planned_lanes, start=1)
        if lane
    ]
    _emit(progress_callback, "translation_lanes_planned", lanes=lane_plan)
    initial_started = time.monotonic()
    translated = _run_fixed_lane_phase(
        batches,
        concurrency=concurrency,
        phase="translate",
        start_event="translation_batch_started",
        progress_event="translation_batch_completed",
        progress_callback=progress_callback,
        run_batch=lambda batch: _run_batch_resilient(
            batch,
            phase="translate",
            cache_dir=cache_dir,
            provider=provider,
            model=model,
            temperature=temperature,
            retries=retries,
            force=force,
            messages_factory=lambda subset: _translation_messages(
                subset,
                profile,
                global_context,
                _neighbor_cues(source, subset),
            ),
        ),
    )
    phase_wall_seconds["initial_translation"] = round(
        time.monotonic() - initial_started, 3
    )
    _validate_phase_map(source, translated, "initial translation")
    initial_translation = dict(translated)

    if polish:
        polish_started = time.monotonic()
        polished = _run_fixed_lane_phase(
            batches,
            concurrency=concurrency,
            phase="polish",
            start_event="polish_batch_started",
            progress_event="polish_batch_completed",
            progress_callback=progress_callback,
            run_batch=lambda batch: _run_batch_resilient(
                batch,
                phase="polish",
                cache_dir=cache_dir,
                provider=provider,
                model=model,
                temperature=temperature,
                retries=retries,
                force=force,
                messages_factory=lambda subset: _polish_messages(
                    subset,
                    initial_translation,
                    profile,
                    global_context,
                    _neighbor_cues(source, subset),
                ),
            ),
        )
        _validate_phase_map(source, polished, "polish")
        translated.update(polished)
        phase_wall_seconds["polish"] = round(
            time.monotonic() - polish_started, 3
        )
    else:
        phase_wall_seconds["polish"] = 0.0

    initial_kana_ids = [
        cue.index for cue in source if KANA_RE.search(translated[cue.index])
    ]
    if initial_kana_ids:
        _emit(
            progress_callback,
            "kana_repair_started",
            current=0,
            total=len(initial_kana_ids),
        )
        residue = [cue for cue in source if cue.index in set(initial_kana_ids)]
        repaired = _run_batch_resilient(
            residue,
            phase="kana_repair",
            cache_dir=cache_dir,
            provider=provider,
            model=model,
            temperature=temperature,
            retries=retries,
            force=force if provider.provider_id == "codex" else True,
            messages_factory=lambda subset: _repair_messages(
                subset, translated, source, profile, global_context
            ),
        )
        translated.update(repaired)
        _emit(
            progress_callback,
            "kana_repair_completed",
            current=len(initial_kana_ids),
            total=len(initial_kana_ids),
        )

    final_kana_ids = [cue.index for cue in source if KANA_RE.search(translated[cue.index])]
    warnings: list[str] = []
    if final_kana_ids:
        warnings.append(
            "一次定点修复后仍有字幕含日文假名；未继续循环修复，"
            f"请人工复核 ID：{final_kana_ids}"
        )
    candidate = [
        SubtitleCue(cue.index, cue.start_ms, cue.end_ms, translated[cue.index])
        for cue in source
    ]
    _validate_final(source, candidate)

    report: dict[str, Any] = {
        "status": "passed",
        "input": str(Path(input_srt)),
        "output": str(output),
        "profile": profile.profile_id,
        "provider": provider.provider_id,
        "provider_identity": provider.cache_identity,
        "model": model,
        "source_sha256": digest,
        "subtitle_count": len(source),
        "translated_count": len(candidate),
        "missing_ids": [],
        "extra_ids": [],
        "empty_ids": [],
        "initial_kana_residue_ids": initial_kana_ids,
        "final_kana_residue_ids": final_kana_ids,
        "warnings": warnings,
        "global_context": bool(global_context),
        "polish": polish,
        "concurrency": concurrency,
        "lane_strategy": "fixed_contiguous_ranges_balanced_by_source_characters",
        "lanes": lane_plan,
        "phase_wall_seconds": phase_wall_seconds,
        "context_strategy": TRANSLATION_CONTEXT_STRATEGY,
        "cache_dir": str(cache_dir),
    }
    if provider.provider_id in {"codex", "qwen", "kimi", "openai", "anthropic", "gemini"}:
        report["output_sha256"] = subtitle_digest(candidate)
    if provider.provider_id == "codex":
        _check_cancelled(provider)
        report["uncertainties"] = [
            {"id": item["id"], "phase": path.parent.name, "note": item["note"]}
            for path in sorted((cache_dir / "batches").glob("*/*.json"))
            for item in json.loads(path.read_text(encoding="utf-8"))["items"]
            if item.get("note", "").strip()
        ]
        summary = getattr(provider, "execution_summary", None)
        if callable(summary):
            report["codex_execution"] = summary()

    temporary = output.with_suffix(output.suffix + f".{uuid.uuid4().hex}.tmp")
    report_temporary = (
        target_report.with_suffix(target_report.suffix + f".{uuid.uuid4().hex}.tmp")
        if target_report is not None
        else None
    )
    output_published = False
    try:
        write_srt(temporary, candidate)
        reparsed = parse_srt(temporary)
        _validate_final(source, reparsed)
        if report_temporary is not None:
            report_temporary.parent.mkdir(parents=True, exist_ok=True)
            report_temporary.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        # os.rename is non-overwriting on the supported Windows production host.
        # Re-check immediately beforehand as a clear guard against late arrivals.
        if output.exists():
            raise FileExistsError(f"refusing to overwrite translated SRT: {output}")
        os.rename(temporary, output)
        output_published = True
        if target_report is not None and report_temporary is not None:
            if target_report.exists():
                raise FileExistsError(
                    f"refusing to overwrite translation report: {target_report}"
                )
            os.rename(report_temporary, target_report)
    except Exception:
        if output_published and output.exists():
            output.unlink()
        raise
    finally:
        if temporary.exists():
            temporary.unlink()
        if report_temporary is not None and report_temporary.exists():
            report_temporary.unlink()
    _emit(progress_callback, "translation_completed", output=str(output), warnings=warnings)
    return report
