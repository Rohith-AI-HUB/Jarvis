from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from system.config import settings


COMMON_REPAIRS = (
    (r"^(?:6|six|sick|seek|fixed|fixes|figs|ficks) yourself\b", "fix yourself"),
    (r"^diagnosed yourself\b", "diagnose yourself"),
    (r"^repair your self\b", "repair yourself"),
    (r"^fix your self\b", "fix yourself"),
    (r"^no thanks\b", "no"),
    (r"^no thank you\b", "no"),
    (r"^not really\b", "no"),
    (r"^what if\b", "what is"),
    (r"^who's\b", "who is"),
    (r"^whats\b", "what is"),
    (r"^google chrome\b", "open chrome"),
    (r"\bvs code\b", "vscode"),
    (r"\bvisual studio code\b", "vscode"),
)

ACTION_STARTERS = (
    "open", "launch", "start", "run", "search", "focus",
    "find", "show", "list", "delete", "move", "copy", "rename",
)


@dataclass(slots=True)
class NormalizedTranscript:
    raw_text: str
    text: str
    changed: bool


def _clean_spacing(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text


def _repair_common_phrases(text: str) -> str:
    updated = text.lower()
    for pattern, replacement in COMMON_REPAIRS:
        updated = re.sub(pattern, replacement, updated)
    return updated


def _normalize_app_reference(text: str) -> str:
    lowered = text.lower().strip()
    tokens = lowered.split()
    if not tokens:
        return lowered
    if tokens[0] not in ACTION_STARTERS or len(tokens) < 2:
        return lowered
    app_phrase = " ".join(tokens[1:])
    configured_apps = sorted(settings.app_registry.keys())
    match = difflib.get_close_matches(app_phrase, configured_apps, n=1, cutoff=0.84)
    if not match:
        return lowered
    return f"{tokens[0]} {match[0]}"


def normalize_transcript(text: str) -> NormalizedTranscript:
    raw_text = _clean_spacing(text)
    normalized = _repair_common_phrases(raw_text)
    normalized = _normalize_app_reference(normalized)
    normalized = _clean_spacing(normalized)
    return NormalizedTranscript(raw_text=raw_text, text=normalized, changed=normalized != raw_text.lower())
