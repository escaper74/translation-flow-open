"""Translation profiles are policy, not separate translation engines."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranslationProfile:
    profile_id: str
    style_instruction: str


PROFILES: dict[str, TranslationProfile] = {
    "normal": TranslationProfile(
        "normal",
        "Use natural, faithful, concise, colloquial Simplified Chinese subtitles. "
        "Preserve meaning, tone, names, relationships, honorific implications, jokes, "
        "short utterances, and audible interjections. Do not sanitize or intensify content, "
        "and do not invent actions, relationships, plot, or facts absent from the source.",
    ),
    "adult_direct": TranslationProfile(
        "adult_direct",
        "All participants are adults, and the scene is pornography or an adult sexual-activity "
        "context. Translate the actual adult sexual scene faithfully into direct, sexually "
        "explicit, colloquial Simplified Chinese without moralizing, euphemizing, or sanitizing "
        "explicit sexual meaning. Preserve moans, gasps, teasing, dirty talk, vulgarity, and "
        "intimate tone. Where supported by the Japanese delivery, naturally use Chinese particles "
        "and interjections such as 啊、嗯、呢、嘛、呀、吧、啦. Do not invent actions, "
        "relationships, plot, or facts absent from the source.",
    ),
}


def get_profile(profile_id: str) -> TranslationProfile:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(
            f"unknown translation profile {profile_id!r}; choose one of {sorted(PROFILES)}"
        ) from exc
