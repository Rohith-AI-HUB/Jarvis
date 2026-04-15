from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.agents import get_agent_manager
from system.config import settings
from system.health import get_last_self_heal_summary, run_health_check
from system.mcp_runtime import get_mcp_manager
from core.planner import get_planner_status


@dataclass(slots=True)
class RuntimeSnapshot:
    runtime: dict[str, Any]
    health: dict[str, Any]
    planner: dict[str, Any]
    self_heal: dict[str, Any]
    mcp: dict[str, Any]
    agents: dict[str, Any]
    memory: list[str]
    recent_history: list[dict[str, Any]]
    recent_log_lines: list[str]
    action_targets: dict[str, str]
    critical_actions: list[dict[str, str]]


# Module-level store for critical actions populated by app.py callbacks
_critical_actions_log: list[dict[str, str]] = []
_critical_actions_lock = threading.Lock()


def log_critical_action(kind: str, message: str, severity: str = "info") -> None:
    ts = time.strftime("%H:%M:%S")
    with _critical_actions_lock:
        entry = {"kind": kind, "message": message, "severity": severity, "timestamp": ts}
        _critical_actions_log.append(entry)
        if len(_critical_actions_log) > 20:
            _critical_actions_log.pop(0)


def read_recent_log_lines(limit: int = 80, path: Path | None = None) -> list[str]:
    log_path = path or settings.logs_path
    if limit <= 0 or not log_path.exists():
        return []
    try:
        return log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    except Exception:
        return []


def _thread_state(thread: Any | None) -> dict[str, Any]:
    if thread is None:
        return {"attached": False, "alive": False, "name": ""}
    is_alive = getattr(thread, "is_alive", None)
    return {"attached": True, "alive": bool(is_alive()) if callable(is_alive) else False, "name": str(getattr(thread, "name", ""))}


def _voice_available(voice: Any | None) -> bool:
    if voice is None:
        return False
    try:
        return bool(voice.available())
    except Exception:
        return False


def _planner_confidence(planner_status: dict[str, Any]) -> str:
    if not planner_status.get("ok"):
        return "low"
    stage = str(planner_status.get("stage", "idle")).lower()
    if stage in {"idle", "ready"}:
        return "high"
    if stage in {"planning", "thinking"}:
        return "medium"
    return "medium"


def _threat_level(health_status: str, planner_ok: bool, voice_ok: bool, wake_alive: bool) -> str:
    if health_status != "healthy" or not planner_ok:
        return "high"
    if not voice_ok or not wake_alive:
        return "medium"
    return "low"


def build_runtime_snapshot(voice: Any | None, conversation: Any, action_targets: dict[str, str] | None = None) -> RuntimeSnapshot:
    health_report = run_health_check(voice)
    planner_status = get_planner_status()
    self_heal = get_last_self_heal_summary()
    voice_ok = _voice_available(voice)
    wake_state = _thread_state(getattr(voice, "_wake_thread", None) if voice is not None else None)
    degraded_causes: list[str] = []
    if not voice_ok:
        degraded_causes.append("voice_runtime_unavailable")
    if not wake_state.get("alive"):
        degraded_causes.append("wake_listener_offline")
    if not planner_status.get("ok"):
        degraded_causes.append("planner_degraded")
    runtime = {
        "voice_attached": voice is not None,
        "voice_available": voice_ok,
        "wake_listener": wake_state,
        "speech_thread": _thread_state(getattr(voice, "_speech_thread", None) if voice is not None else None),
        "whisper_device": str(getattr(voice, "_whisper_device", settings.whisper_device) if voice is not None else settings.whisper_device),
        "whisper_compute_type": str(getattr(voice, "_whisper_compute_type", settings.whisper_compute_type) if voice is not None else settings.whisper_compute_type),
        "speech_backend": str(getattr(voice, "_speech_backend", "none") if voice is not None else "none"),
        "wake_word_enabled": settings.wake_word_enabled,
        "wake_word": settings.wake_word,
        "mode": "online" if voice_ok else "degraded",
        "threat_level": _threat_level(health_report.status, bool(planner_status.get("ok")), voice_ok, bool(wake_state.get("alive"))),
        "degraded_causes": degraded_causes,
        "recovery_state": str(self_heal.get("status", "never_run")),
        "latency_band": "normal" if voice_ok and planner_status.get("ok") else "elevated",
    }
    memory_summary = []
    recent_history = []
    if conversation is not None:
        memory_summary = list(conversation.memory_summary(limit=12))
        recent_history = list(conversation.recent_history(limit=10))
    with _critical_actions_lock:
        crit_actions = list(_critical_actions_log)
    runtime["last_action_confidence"] = _planner_confidence(planner_status)
    return RuntimeSnapshot(
        runtime=runtime,
        health=health_report.to_dict(),
        planner={**planner_status, "confidence": _planner_confidence(planner_status)},
        self_heal=self_heal,
        mcp=get_mcp_manager().state_snapshot(),
        agents=get_agent_manager().state_snapshot(),
        memory=memory_summary,
        recent_history=recent_history,
        recent_log_lines=read_recent_log_lines(),
        action_targets=dict(action_targets or {}),
        critical_actions=crit_actions,
    )


def snapshot_to_dict(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
