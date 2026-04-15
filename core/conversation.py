from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.config import settings

try:
    from semantic_memory import SemanticMemory
except ImportError:
    SemanticMemory = None


LOGGER = logging.getLogger(__name__)

MEMORY_CATEGORIES = ("user_profile", "assistant_identity", "preferences", "projects", "corrections", "facts")
LEGACY_CATEGORY_MAP = {"profile": "user_profile", "goals": "projects"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("Failed to read JSON from %s", path)
        return default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _clean_value(value: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip(" .,!?:;\"'"))
    return cleaned[:max_len].strip()


def _title_case_name(value: str) -> str:
    parts = [part for part in re.split(r"\s+", value.strip(" .,!?:;")) if part]
    return " ".join(part[:1].upper() + part[1:].lower() for part in parts[:3])


def _normalized_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s'-]+", " ", value.lower()).split())


def _slug(value: str, fallback: str = "memory") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (slug or fallback)[:64]


def _human_label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize() or "Memory"


def _default_memory_data() -> dict[str, Any]:
    data: dict[str, Any] = {"version": 2, "updated_at": _utcnow_iso(), "last_write": None}
    for category in MEMORY_CATEGORIES:
        data[category] = {}
    return data


def _coerce_memory_item(category: str, value: Any, source: str = "") -> dict[str, Any] | None:
    if isinstance(value, dict):
        item_value = _clean_value(str(value.get("value", "")))
        if not item_value:
            return None
        created_at = str(value.get("created_at") or value.get("updated_at") or _utcnow_iso())
        updated_at = str(value.get("updated_at") or created_at)
        item_source = str(value.get("source") or source)
    else:
        item_value = _clean_value(str(value))
        if not item_value:
            return None
        created_at = _utcnow_iso()
        updated_at = created_at
        item_source = source
    return {"value": item_value, "source": item_source, "category": category, "confidence": "explicit", "created_at": created_at, "updated_at": updated_at}


@dataclass(slots=True)
class MemoryWriteResult:
    ok: bool
    message: str
    category: str = ""
    key: str = ""
    value: str = ""


@dataclass(slots=True)
class ConversationTurn:
    role: str
    text: str
    kind: str
    created_at: str


@dataclass(slots=True)
class ConversationContext:
    memory_lines: list[str]
    recent_turns: list[ConversationTurn]


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        loaded = _read_json(self._path, _default_memory_data())
        data = _default_memory_data()
        for category in MEMORY_CATEGORIES:
            self._merge_category(data, category, loaded.get(category, {}))
        for legacy_category, category in LEGACY_CATEGORY_MAP.items():
            self._merge_category(data, category, loaded.get(legacy_category, {}))
        legacy_facts = loaded.get("facts", {})
        if isinstance(legacy_facts, dict):
            current_project = legacy_facts.get("current_project")
            if current_project is not None:
                item = _coerce_memory_item("projects", current_project)
                if item:
                    data["projects"].setdefault("current_project", item)
            for key, value in legacy_facts.items():
                if key == "current_project":
                    continue
                item = _coerce_memory_item("facts", value)
                if item:
                    data["facts"].setdefault(_slug(key), item)
        last_write = loaded.get("last_write")
        if isinstance(last_write, dict):
            category = self._normalize_category(str(last_write.get("category", "")))
            key = _slug(str(last_write.get("key", "")), fallback="")
            if category and key and key in data.get(category, {}):
                data["last_write"] = {"category": category, "key": key}
        data["updated_at"] = str(loaded.get("updated_at") or _utcnow_iso())
        return data

    def _merge_category(self, data: dict[str, Any], category: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        for raw_key, raw_value in payload.items():
            key = _slug(str(raw_key))
            item = _coerce_memory_item(category, raw_value)
            if item:
                data[category][key] = item

    def _normalize_category(self, category: str) -> str:
        normalized = _slug(category)
        normalized = LEGACY_CATEGORY_MAP.get(normalized, normalized)
        if normalized not in MEMORY_CATEGORIES:
            return "facts"
        return normalized

    def _remember_unlocked(self, category: str, key: str, value: str, source: str) -> MemoryWriteResult:
        normalized_category = self._normalize_category(category)
        normalized_key = _slug(key)
        cleaned_value = _clean_value(value)
        if not cleaned_value:
            return MemoryWriteResult(False, "I did not find anything clear to remember.", normalized_category, normalized_key)
        now = _utcnow_iso()
        previous = self._data[normalized_category].get(normalized_key, {})
        created_at = str(previous.get("created_at") or now) if isinstance(previous, dict) else now
        self._data[normalized_category][normalized_key] = {"value": cleaned_value, "source": source, "category": normalized_category, "confidence": "explicit", "created_at": created_at, "updated_at": now}
        self._data["last_write"] = {"category": normalized_category, "key": normalized_key}
        self._data["updated_at"] = now
        return MemoryWriteResult(True, f"Remembered {_human_label(normalized_key)}.", normalized_category, normalized_key, cleaned_value)

    def remember(self, category: str, key: str, value: str, source: str) -> MemoryWriteResult:
        with self._lock:
            result = self._remember_unlocked(category, key, value, source)
            if result.ok:
                _write_json(self._path, self._data)
            return result

    def forget(self, category: str, key: str | None = None) -> MemoryWriteResult:
        with self._lock:
            normalized_category = self._normalize_category(category)
            bucket = self._data.get(normalized_category, {})
            if key is None:
                if not bucket:
                    return MemoryWriteResult(False, "I do not have that memory saved.", normalized_category)
                bucket.clear()
                self._data["last_write"] = None
                self._data["updated_at"] = _utcnow_iso()
                _write_json(self._path, self._data)
                return MemoryWriteResult(True, f"Forgot all {_human_label(normalized_category)} memories.", normalized_category)
            normalized_key = _slug(key)
            if normalized_key not in bucket:
                return MemoryWriteResult(False, "I do not have that memory saved.", normalized_category, normalized_key)
            del bucket[normalized_key]
            if self._data.get("last_write") == {"category": normalized_category, "key": normalized_key}:
                self._data["last_write"] = None
            self._data["updated_at"] = _utcnow_iso()
            _write_json(self._path, self._data)
            return MemoryWriteResult(True, f"Forgot {_human_label(normalized_key)}.", normalized_category, normalized_key)

    def forget_last(self) -> MemoryWriteResult:
        with self._lock:
            last_write = self._data.get("last_write")
            if not isinstance(last_write, dict):
                return MemoryWriteResult(False, "I do not have a recent memory to forget.")
            category = self._normalize_category(str(last_write.get("category", "")))
            key = _slug(str(last_write.get("key", "")), fallback="")
            if not key:
                return MemoryWriteResult(False, "I do not have a recent memory to forget.")
            return self.forget(category, key)

    def get(self, category: str, key: str) -> str | None:
        with self._lock:
            normalized_category = self._normalize_category(category)
            item = self._data.get(normalized_category, {}).get(_slug(key), {})
            if isinstance(item, dict):
                value = str(item.get("value", "")).strip()
                return value or None
            return None

    def learn_from_user_message(self, text: str) -> list[MemoryWriteResult]:
        original = text.strip()
        if not original:
            return []
        with self._lock:
            results = self._learn_explicit_unlocked(original)
            if results:
                _write_json(self._path, self._data)
            return results

    def _learn_explicit_unlocked(self, original: str) -> list[MemoryWriteResult]:
        results: list[MemoryWriteResult] = []
        direct_result = self._learn_direct_statement_unlocked(original)
        if direct_result:
            results.append(direct_result)
        correction_result = self._learn_correction_unlocked(original)
        if correction_result:
            results.append(correction_result)
        remember_match = re.search(r"^\s*(?:please\s+)?remember(?: that)?\s+(.+)$", original, flags=re.IGNORECASE)
        if remember_match:
            remembered = _clean_value(remember_match.group(1))
            if remembered:
                nested = self._learn_direct_statement_unlocked(remembered) or self._learn_correction_unlocked(remembered)
                if nested:
                    results.append(nested)
                else:
                    results.append(self._remember_statement_unlocked(remembered, original))
        from_now_match = re.search(r"\bfrom now on\s+(.+)$", original, flags=re.IGNORECASE)
        if from_now_match:
            instruction = _clean_value(from_now_match.group(1))
            nested = self._learn_direct_statement_unlocked(instruction)
            if nested:
                results.append(nested)
            elif instruction:
                key = f"instruction_{_slug(instruction)}"
                results.append(self._remember_unlocked("preferences", key, instruction, original))
        preference_match = re.search(r"\bi prefer\s+(.+)$", original, flags=re.IGNORECASE)
        if preference_match:
            value = _clean_value(preference_match.group(1))
            if value:
                results.append(self._remember_unlocked("preferences", _slug(value, "preference"), value, original))
        want_you_match = re.search(r"\bi want you to\s+(.+)$", original, flags=re.IGNORECASE)
        if want_you_match:
            value = _clean_value(want_you_match.group(1))
            if value:
                results.append(self._remember_unlocked("preferences", f"instruction_{_slug(value)}", value, original))
        return self._dedupe_results(results)

    def _learn_direct_statement_unlocked(self, original: str) -> MemoryWriteResult | None:
        patterns: tuple[tuple[str, str, str, bool], ...] = (
            (r"\bmy name is\s+([^,.!?]+)", "user_profile", "preferred_name", True),
            (r"\bcall me\s+([^,.!?]+)", "user_profile", "preferred_name", True),
            (r"\byour name is\s+([^,.!?]+)", "assistant_identity", "name", True),
            (r"\bcall yourself\s+([^,.!?]+)", "assistant_identity", "name", True),
            (r"\brefer to yourself as\s+([^,.!?]+)", "assistant_identity", "name", True),
            (r"\bmy project is\s+([^,.!?]+)", "projects", "current_project", False),
            (r"\bcurrent project is\s+([^,.!?]+)", "projects", "current_project", False),
        )
        for pattern, category, key, is_name in patterns:
            match = re.search(pattern, original, flags=re.IGNORECASE)
            if not match:
                continue
            value = _clean_value(match.group(1))
            if is_name:
                value = re.split(r"\b(?:and|but|because|so)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
                value = _title_case_name(value)
            return self._remember_unlocked(category, key, value, original)
        return None

    def _learn_correction_unlocked(self, original: str) -> MemoryWriteResult | None:
        when_i_say = re.search(r"\bwhen i say\s+(.+?)\s+i mean\s+(.+?)(?:[.!?]|$)", original, flags=re.IGNORECASE)
        if when_i_say:
            heard = _clean_value(when_i_say.group(1))
            meant = _clean_value(when_i_say.group(2))
            if heard and meant:
                return self._remember_unlocked("corrections", _slug(heard), meant, original)
        i_meant = re.search(r"\bi meant\s+(.+?)\s+not\s+(.+?)(?:[.!?]|$)", original, flags=re.IGNORECASE)
        if i_meant:
            meant = _clean_value(i_meant.group(1))
            heard = _clean_value(i_meant.group(2))
            if heard and meant:
                return self._remember_unlocked("corrections", _slug(heard), meant, original)
        return None

    def _remember_statement_unlocked(self, remembered: str, source: str) -> MemoryWriteResult:
        direct_result = self._learn_direct_statement_unlocked(remembered)
        if direct_result:
            return direct_result
        lowered = remembered.lower()
        favorite_match = re.search(r"\bmy\s+(.+?)\s+is\s+(.+)$", remembered, flags=re.IGNORECASE)
        if favorite_match and "name" not in favorite_match.group(1).lower():
            key = _slug(favorite_match.group(1))
            value = _clean_value(favorite_match.group(2))
            return self._remember_unlocked("facts", key, value, source)
        if "project" in lowered or "working on" in lowered or "building" in lowered:
            value = re.sub(r"^(?:we are|we're|i am|i'm)?\s*(?:working on|building)\s+", "", remembered, flags=re.IGNORECASE)
            return self._remember_unlocked("projects", "current_project", _clean_value(value), source)
        if lowered.startswith("i prefer ") or lowered.startswith("prefer "):
            value = re.sub(r"^(?:i\s+)?prefer\s+", "", remembered, flags=re.IGNORECASE)
            return self._remember_unlocked("preferences", _slug(value, "preference"), _clean_value(value), source)
        if lowered.startswith("i want you to "):
            value = re.sub(r"^i want you to\s+", "", remembered, flags=re.IGNORECASE)
            return self._remember_unlocked("preferences", f"instruction_{_slug(value)}", _clean_value(value), source)
        return self._remember_unlocked("facts", f"remembered_{_slug(remembered)}", remembered, source)

    def _dedupe_results(self, results: list[MemoryWriteResult]) -> list[MemoryWriteResult]:
        deduped: list[MemoryWriteResult] = []
        seen: set[tuple[str, str]] = set()
        for result in results:
            marker = (result.category, result.key)
            if not result.ok or marker in seen:
                continue
            seen.add(marker)
            deduped.append(result)
        return deduped

    def apply_corrections(self, text: str) -> str:
        with self._lock:
            corrections = list(self._data.get("corrections", {}).items())
        corrected = text
        for key, item in sorted(corrections, key=lambda pair: len(pair[0]), reverse=True):
            if not isinstance(item, dict):
                continue
            replacement = str(item.get("value", "")).strip()
            if not replacement:
                continue
            phrase = key.replace("_", " ")
            pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", flags=re.IGNORECASE)
            corrected = pattern.sub(replacement, corrected)
        return corrected

    def summary(self, limit: int = 12) -> list[str]:
        with self._lock:
            lines: list[str] = []
            preferred_name = self.get("user_profile", "preferred_name")
            if preferred_name:
                lines.append(f"Your name: {preferred_name}")
            assistant_name = self.get("assistant_identity", "name")
            if assistant_name:
                lines.append(f"Assistant name: {assistant_name}")
            project = self.get("projects", "current_project")
            if project:
                lines.append(f"Current project: {project}")
            current_goal = self.get("projects", "current_goal")
            if current_goal:
                lines.append(f"Current goal: {current_goal}")
            for category, prefix in (("preferences", "Preference"), ("corrections", "Correction"), ("facts", "Fact")):
                for key, item in self._data.get(category, {}).items():
                    if not isinstance(item, dict):
                        continue
                    value = str(item.get("value", "")).strip()
                    if not value:
                        continue
                    if category == "corrections":
                        lines.append(f"{prefix}: {key.replace('_', ' ')} means {value}")
                    elif key.startswith("remembered_"):
                        lines.append(f"{prefix}: {value}")
                    else:
                        lines.append(f"{prefix}: {_human_label(key)} = {value}")
                    if len(lines) >= limit:
                        return lines[:limit]
            return lines[:limit]

    def lines(self, limit: int = 8) -> list[str]:
        lines: list[str] = []
        assistant_name = self.get("assistant_identity", "name")
        if assistant_name:
            lines.append(f"Assistant identity: Your name is {assistant_name}. Always identify yourself as {assistant_name}; never use the underlying model name.")
        for line in self.summary(limit=limit + 2):
            if line.startswith("Assistant name:") and assistant_name:
                continue
            lines.append(line)
            if len(lines) >= limit:
                break
        return lines[:limit]


LocalMemoryStore = MemoryStore


class ConversationManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory = MemoryStore(settings.memory_profile_path)
        self._history = deque(maxlen=settings.conversation_history_limit)
        self._load_history()
        self._semantic_memory: SemanticMemory | None = None
        if SemanticMemory is not None:
            try:
                model = settings.semantic_embedding_model or settings.ollama_model
                self._semantic_memory = SemanticMemory(
                    index_path=settings.semantic_index_path,
                    ollama_base_url=settings.ollama_base_url,
                    model=model,
                    turns_per_chunk=settings.semantic_chunk_size,
                    overlap=settings.semantic_chunk_overlap,
                )
                LOGGER.info("Semantic memory initialized at %s", settings.semantic_index_path)
            except Exception:
                LOGGER.warning("Failed to initialize semantic memory", exc_info=True)

    def _load_history(self) -> None:
        default = {"turns": []}
        payload = _read_json(settings.conversation_history_path, default)
        turns = payload.get("turns", [])
        for item in turns[-settings.conversation_history_limit:]:
            if not isinstance(item, dict):
                continue
            try:
                turn = ConversationTurn(role=str(item.get("role", "assistant")), text=str(item.get("text", "")).strip(), kind=str(item.get("kind", "conversation")), created_at=str(item.get("created_at", _utcnow_iso())))
            except Exception:
                continue
            if turn.text:
                self._history.append(turn)

    def _save_history(self) -> None:
        payload = {"turns": [asdict(turn) for turn in self._history]}
        _write_json(settings.conversation_history_path, payload)

    def prepare_context(self, user_text: str) -> ConversationContext:
        self._memory.learn_from_user_message(user_text)
        with self._lock:
            recent_turns = list(self._history)[-settings.conversation_prompt_turn_limit:]
        return ConversationContext(memory_lines=self._memory.lines(), recent_turns=recent_turns)

    def handle_memory_command(self, text: str) -> dict[str, Any] | None:
        normalized = _normalized_text(text)
        if not normalized:
            return None
        if normalized in {"what do you remember", "what do you remember about me", "show memory", "show memories"}:
            lines = self._memory.summary()
            message = "I remember: " + "; ".join(lines) if lines else "I do not have any saved memories yet."
            return {"status": "answered", "message": message, "results": []}
        if normalized == "forget that":
            return self._memory_result_to_command(self._memory.forget_last())
        forget_map = {"forget my name": ("user_profile", "preferred_name"), "forget your name": ("assistant_identity", "name"), "forget my project": ("projects", "current_project")}
        if normalized in forget_map:
            category, key = forget_map[normalized]
            return self._memory_result_to_command(self._memory.forget(category, key))
        if normalized == "clear conversation history":
            self.clear_history()
            return {"status": "completed", "message": "Conversation history cleared.", "results": [], "skip_history_record": True}
        results = self._memory.learn_from_user_message(text)
        if not results:
            return None
        message = "Remembered: " + "; ".join(self._spoken_memory_result(result) for result in results)
        return {"status": "completed", "message": message, "results": [asdict(result) for result in results]}

    def _memory_result_to_command(self, result: MemoryWriteResult) -> dict[str, Any]:
        return {"status": "completed" if result.ok else "failed", "message": result.message, "results": [asdict(result)]}

    def _spoken_memory_result(self, result: MemoryWriteResult) -> str:
        if result.category == "assistant_identity" and result.key == "name":
            return f"my name is {result.value}"
        if result.category == "user_profile" and result.key == "preferred_name":
            return f"your name is {result.value}"
        if result.category == "corrections":
            return f"{result.key.replace('_', ' ')} means {result.value}"
        if result.category == "projects" and result.key == "current_project":
            return f"current project is {result.value}"
        return f"{_human_label(result.key)} is {result.value}"

    def apply_memory_corrections(self, text: str) -> str:
        return self._memory.apply_corrections(text)

    def answer_from_memory(self, text: str) -> str | None:
        normalized = _normalized_text(text)
        if normalized in {"what is your name", "whats your name", "who are you", "tell me your name"}:
            name = self._memory.get("assistant_identity", "name") or "Jarvis"
            return f"My name is {name}."
        if normalized in {"what is my name", "whats my name", "who am i"}:
            name = self._memory.get("user_profile", "preferred_name")
            if name:
                return f"Your name is {name}."
            return "I do not have your name saved yet."
        return None

    def answer_from_semantic_memory(self, text: str) -> str | None:
        if self._semantic_memory is None:
            return None
        try:
            return self._semantic_memory.answer_query(text, top_k=settings.semantic_top_k)
        except Exception:
            LOGGER.warning("Semantic memory query failed", exc_info=True)
            return None

    def memory_summary(self, limit: int = 12) -> list[str]:
        return self._memory.summary(limit=limit)

    def recent_history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            turns = list(self._history)[-limit:]
        return [asdict(turn) for turn in turns]

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
            self._save_history()

    def record_exchange(self, user_text: str, assistant_text: str, assistant_kind: str = "conversation") -> None:
        timestamp = _utcnow_iso()
        user_turn = ConversationTurn(role="user", text=user_text.strip(), kind="user", created_at=timestamp)
        assistant_turn = ConversationTurn(role="assistant", text=assistant_text.strip(), kind=assistant_kind, created_at=_utcnow_iso())
        with self._lock:
            if user_turn.text:
                self._history.append(user_turn)
            if assistant_turn.text:
                self._history.append(assistant_turn)
            self._save_history()
        if self._semantic_memory is not None:
            try:
                self._semantic_memory.index_exchange(user_text, assistant_text, assistant_kind, timestamp)
            except Exception:
                LOGGER.warning("Failed to index exchange in semantic memory", exc_info=True)

    def record_assistant_event(self, assistant_text: str, assistant_kind: str = "event") -> None:
        timestamp = _utcnow_iso()
        turn = ConversationTurn(role="assistant", text=assistant_text.strip(), kind=assistant_kind, created_at=timestamp)
        with self._lock:
            if turn.text:
                self._history.append(turn)
                self._save_history()
        if self._semantic_memory is not None:
            try:
                self._semantic_memory.index_exchange("", assistant_text, assistant_kind, timestamp)
            except Exception:
                LOGGER.warning("Failed to index assistant event in semantic memory", exc_info=True)
