from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from core.agents import get_agent_manager
from system.config import settings
from system.mcp_runtime import get_mcp_manager
from core.planner import get_planner_status


LOGGER = logging.getLogger(__name__)
RepairPolicy = Literal["safe_auto", "ask_first", "diagnostics_only"]
_LAST_SELF_HEAL_SUMMARY: dict[str, Any] = {"status": "never_run", "message": "", "updated_at": ""}


@dataclass(slots=True)
class HealthCheckResult:
    name: str
    ok: bool
    message: str
    severity: str = "info"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RepairResult:
    name: str
    ok: bool
    message: str
    changed: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HealthReport:
    status: str
    message: str
    checks: list[HealthCheckResult] = field(default_factory=list)
    repairs: list[RepairResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and all(repair.ok for repair in self.repairs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_command_result(self) -> dict[str, Any]:
        results = [{"message": repair.message, "data": repair.details} for repair in self.repairs if repair.changed]
        if not results:
            results = [{"message": check.message, "data": check.details} for check in self.checks if not check.ok]
        return {"status": self.status, "message": self.message, "results": results, "health": self.to_dict()}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_log_checks(checks: list[HealthCheckResult]) -> None:
    if settings.logs_path.exists():
        checks.append(HealthCheckResult("logs", True, "Recent log data is available.", details={"path": str(settings.logs_path)}))
    else:
        checks.append(HealthCheckResult("logs", True, "No recent log data was available."))


def _voice_dependency_status() -> dict[str, bool]:
    try:
        from voice.voice import voice_dependency_status
        return voice_dependency_status()
    except Exception:
        return {"voice_module": False}


def _add_voice_dependency_checks(checks: list[HealthCheckResult]) -> None:
    dependencies = _voice_dependency_status()
    missing = sorted(name for name, ok in dependencies.items() if not ok)
    checks.append(HealthCheckResult(
        "voice_dependencies",
        not missing,
        "Voice dependencies are available." if not missing else f"Missing voice dependencies: {', '.join(missing)}.",
        severity="warning" if missing else "info",
        details={"dependencies": dependencies},
    ))


def _add_planner_checks(checks: list[HealthCheckResult]) -> None:
    planner_status = get_planner_status()
    checks.append(HealthCheckResult(
        "planner_status",
        bool(planner_status.get("ok", True)),
        "Planner is healthy." if planner_status.get("ok", True) else str(planner_status.get("message", "Planner failure recorded.")),
        severity="warning" if not planner_status.get("ok", True) else "info",
        details=planner_status,
    ))


def _add_mcp_checks(checks: list[HealthCheckResult]) -> None:
    servers = get_mcp_manager().state_snapshot().get("servers", [])
    disconnected = [server for server in servers if server.get("enabled") and not server.get("connected") and server.get("last_error")]
    checks.append(HealthCheckResult(
        "mcp_servers",
        not disconnected,
        "MCP servers are healthy." if not disconnected else f"{len(disconnected)} MCP server(s) have connection errors.",
        severity="warning" if disconnected else "info",
        details={"servers": servers},
    ))


def _add_agent_checks(checks: list[HealthCheckResult]) -> None:
    agents = get_agent_manager().state_snapshot().get("agents", [])
    failed = [agent for agent in agents if agent.get("status") == "failed"]
    checks.append(HealthCheckResult(
        "managed_agents",
        not failed,
        "Managed agents are healthy." if not failed else f"{len(failed)} managed agent(s) failed.",
        severity="warning" if failed else "info",
        details={"agents": agents},
    ))


def _add_runtime_checks(checks: list[HealthCheckResult], voice: Any | None) -> None:
    if voice is None:
        checks.append(HealthCheckResult("voice_runtime", True, "Voice runtime is not attached. Jarvis is running in degraded text-only mode.", severity="info"))
        return
    try:
        runtime_status = getattr(voice, "runtime_status", lambda: {})()
        checks.append(HealthCheckResult(
            "voice_available",
            bool(voice.available()),
            "Offline voice runtime is available." if voice.available() else "Offline voice runtime is unavailable.",
            severity="error" if not voice.available() else "info",
            details=runtime_status if isinstance(runtime_status, dict) else {},
        ))
    except Exception as exc:
        checks.append(HealthCheckResult("voice_available", False, f"Voice availability check failed: {exc}", severity="error"))
    wake_thread = getattr(voice, "_wake_thread", None)
    speech_thread = getattr(voice, "_speech_thread", None)
    checks.append(HealthCheckResult("wake_listener", wake_thread is None or bool(getattr(wake_thread, "is_alive", lambda: False)()), "Wake listener thread is healthy or idle.", details={"attached": wake_thread is not None}))
    checks.append(HealthCheckResult("speech_thread", speech_thread is None or bool(getattr(speech_thread, "is_alive", lambda: False)()), "Speech thread is healthy or idle.", details={"attached": speech_thread is not None}))


def _add_config_checks(checks: list[HealthCheckResult]) -> None:
    checks.append(HealthCheckResult("whisper_config", True, f"Whisper is configured for {settings.whisper_device}/{settings.whisper_compute_type}.", details={"device": settings.whisper_device, "compute_type": settings.whisper_compute_type}))
    checks.append(HealthCheckResult(
        "piper_voice_files",
        (settings.piper_voice_dir / f"{settings.piper_voice_name}.onnx").exists() and (settings.piper_voice_dir / f"{settings.piper_voice_name}.onnx.json").exists(),
        "Piper voice files are present.",
        severity="warning",
    ))


def run_health_check(voice: Any | None) -> HealthReport:
    checks: list[HealthCheckResult] = []
    _add_log_checks(checks)
    _add_config_checks(checks)
    _add_voice_dependency_checks(checks)
    _add_planner_checks(checks)
    _add_mcp_checks(checks)
    _add_agent_checks(checks)
    _add_runtime_checks(checks, voice)
    issues = [check for check in checks if not check.ok]
    message = "Health check passed." if not issues else f"Health check found {len(issues)} issue(s)."
    return HealthReport(status="healthy" if not issues else "degraded", message=message, checks=checks)


def _repair_whisper_cpu(voice: Any) -> RepairResult:
    if getattr(voice, "_whisper_device", settings.whisper_device) == "cpu" and settings.whisper_device == "cpu":
        return RepairResult("whisper_cpu_fallback", True, "Whisper is already using CPU fallback.", details={"device": "cpu"})
    try:
        switch = getattr(voice, "_switch_whisper_to_cpu")
        switch()
        return RepairResult("whisper_cpu_fallback", True, "Switched Whisper to CPU fallback.", changed=True, details={"device": "cpu"})
    except Exception as exc:
        LOGGER.exception("Whisper CPU fallback repair failed.")
        return RepairResult("whisper_cpu_fallback", False, f"Whisper CPU fallback failed: {exc}")


def _repair_voice_models(voice: Any) -> RepairResult:
    try:
        voice.stop_wake_listener()
        voice.reset_voice_models()
        threading.Thread(target=voice.preload, daemon=True, name="jarvis-health-preload").start()
        return RepairResult("voice_model_reset", True, "Reset voice models and queued preload.", changed=True)
    except Exception as exc:
        LOGGER.exception("Voice model reset repair failed.")
        return RepairResult("voice_model_reset", False, f"Voice model reset failed: {exc}")


def _repair_speech(voice: Any) -> RepairResult:
    try:
        voice.reset_speech()
        return RepairResult("speech_restart", True, "Restarted speech engine.", changed=True)
    except Exception as exc:
        LOGGER.exception("Speech restart repair failed.")
        return RepairResult("speech_restart", False, f"Speech restart failed: {exc}")


def get_last_self_heal_summary() -> dict[str, Any]:
    return dict(_LAST_SELF_HEAL_SUMMARY)


def run_self_heal(voice: Any | None, policy: RepairPolicy = "safe_auto") -> HealthReport:
    report = run_health_check(voice)
    repairs: list[RepairResult] = []
    if policy == "diagnostics_only":
        return HealthReport("self_heal_diagnostics", report.message, checks=report.checks, repairs=repairs)
    if policy == "ask_first":
        return HealthReport("self_heal_needs_confirmation", "I found the likely issues, but repair policy requires confirmation before changes.", checks=report.checks, repairs=repairs)
    if voice is None:
        repairs.append(RepairResult("voice_runtime", False, "Voice runtime is not attached."))
    else:
        checks_by_name = {check.name: check for check in report.checks}
        voice_available = checks_by_name.get("voice_available")
        wake_listener = checks_by_name.get("wake_listener")
        speech_thread = checks_by_name.get("speech_thread")
        voice_details = voice_available.details if voice_available else {}
        missing_dependencies = set(voice_details.get("missing_dependencies", [])) if isinstance(voice_details, dict) else set()
        if "numpy" not in missing_dependencies and "faster_whisper" not in missing_dependencies and settings.whisper_device != "cpu":
            repairs.append(_repair_whisper_cpu(voice))
        if wake_listener and not wake_listener.ok:
            repairs.append(_repair_voice_models(voice))
        if speech_thread and not speech_thread.ok:
            repairs.append(_repair_speech(voice))
    failed = [repair for repair in repairs if not repair.ok]
    changed = [repair for repair in repairs if repair.changed]
    if failed:
        status = "self_heal_partial"
        message = f"I applied {len(changed)} repair(s), but {len(failed)} issue(s) remain."
    elif changed:
        status = "self_healed"
        message = "I checked myself and applied safe repairs."
    else:
        status = "self_healed"
        message = "I checked myself and did not need to change anything."
    LOGGER.info("Self-heal completed: %s", {"status": status, "repairs": [asdict(repair) for repair in repairs]})
    _LAST_SELF_HEAL_SUMMARY.update({"status": status, "message": message, "updated_at": _utcnow_iso()})
    return HealthReport(status, message, checks=report.checks, repairs=repairs)
