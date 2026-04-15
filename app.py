from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

from system.config import settings
from core.conversation import ConversationManager
from interface.control_center import ControlCenterWindow
from core.executor import ExecutionError, execute_plan
from system.health import run_self_heal as health_run_self_heal
from interface.overlay import AssistantHud
from core.planner import PlannerError, answer_conversation, classify_intent, plan_command
from voice.transcript import normalize_transcript

if TYPE_CHECKING:
    from voice import VoiceInterface

try:
    import keyboard
except ImportError:  # pragma: no cover
    keyboard = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None
    Image = None
    ImageDraw = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


LOGGER = logging.getLogger(__name__)
_INSTANCE_MUTEX = None
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
_TRAY_STOP_EVENT = f"{settings.app_name}-tray-stop-event"
_CONVERSATION = ConversationManager()
STOP_LISTENING_TOKENS = {
    "no",
    "nope",
    "nah",
    "stop",
    "cancel",
    "abort",
    "nevermind",
    "never mind",
    "not now",
    "thats all",
    "that's all",
    "im done",
    "i'm done",
}
SELF_HEAL_TOKENS = (
    "fix yourself",
    "repair yourself",
    "diagnose yourself",
    "heal yourself",
    "recover yourself",
)
REFERENCE_WORDS = {"it", "that", "this", "them"}
REFERENCE_VERBS = {"open", "launch", "start", "use", "focus", "run", "delete", "move", "copy", "rename"}
_LAST_ACTION_TARGETS: dict[str, str] = {}


class UnavailableVoiceInterface:
    def __init__(self, reason: str = "Voice runtime unavailable.") -> None:
        self.reason = reason
        self._wake_thread = None
        self._speech_thread = None
        self._whisper_device = settings.whisper_device
        self._whisper_compute_type = settings.whisper_compute_type
        self._speech_backend = "none"

    def available(self) -> bool:
        return False

    def runtime_status(self) -> dict[str, Any]:
        return {
            "available": False,
            "missing_dependencies": [],
            "reason": self.reason,
            "speech_backend": self._speech_backend,
            "whisper_device": self._whisper_device,
            "whisper_compute_type": self._whisper_compute_type,
        }

    def preload(self) -> None:
        return None

    def start_wake_listener(self, *_: Any, **__: Any) -> None:
        return None

    def stop_wake_listener(self) -> None:
        return None

    def reset_voice_models(self) -> None:
        return None

    def reset_speech(self) -> None:
        return None

    def play_listening_cue(self) -> bool:
        return False

    def say(self, _: str) -> bool:
        return False

    def capture_command(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError(self.reason)

    def capture_confirmation(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError(self.reason)

    def shutdown(self) -> None:
        return None


def create_voice_interface() -> Any:
    try:
        from voice import VoiceInterface

        return VoiceInterface()
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Voice runtime failed to initialize.")
        return UnavailableVoiceInterface(str(exc))


class SessionCallbacks(Protocol):
    def on_idle(self) -> None: ...
    def on_wake_detected(self) -> None: ...
    def on_listening(self, level: float) -> None: ...
    def on_thinking(self) -> None: ...
    def on_confirmation(self, prompt: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_complete(self, result: str) -> None: ...


def startup_script_path() -> Path:
    return settings.startup_folder / settings.startup_script_name


def legacy_startup_script_path() -> Path:
    return settings.startup_folder / "jarvis-startup.cmd"


def startup_python_executable() -> Path:
    local_pythonw = Path(sys.executable).with_name("pythonw.exe")
    if local_pythonw.exists():
        return local_pythonw
    return Path(sys.executable)


def install_startup() -> Path:
    settings.startup_folder.mkdir(parents=True, exist_ok=True)
    if legacy_startup_script_path().exists():
        legacy_startup_script_path().unlink()
    script_path = startup_script_path()
    python_executable = startup_python_executable()
    app_path = Path(__file__).resolve()
    command = f'"{python_executable}" "{app_path}" --tray'
    launcher = "\n".join(
        [
            'Set shell = CreateObject("WScript.Shell")',
            f'shell.CurrentDirectory = "{app_path.parent}"',
            f'shell.Run "{command.replace(chr(34), chr(34) + chr(34))}", 0, False',
            "",
        ]
    )
    script_path.write_text(launcher, encoding="utf-8")
    return script_path


def remove_startup() -> bool:
    removed = False
    for script_path in (startup_script_path(), legacy_startup_script_path()):
        if script_path.exists():
            script_path.unlink()
            removed = True
    return removed


def ensure_single_tray_instance() -> bool:
    global _INSTANCE_MUTEX
    mutex_name = f"{settings.app_name}-tray-instance"
    _INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    return ctypes.GetLastError() != ERROR_ALREADY_EXISTS


def stop_running_instances() -> int:
    if not psutil:
        raise RuntimeError("psutil is required to stop running Jarvis instances.")

    current_pid = os.getpid()
    app_name = Path(__file__).name.lower()
    tray_processes = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info["pid"]
            cmdline = [part.lower() for part in (proc.info.get("cmdline") or [])]
            if pid == current_pid:
                continue
            if app_name not in cmdline:
                continue
            if "--tray" in cmdline or "--voice" in cmdline:
                tray_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for proc in tray_processes:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            LOGGER.debug(f"Failed to terminate process {proc.pid}: {e}")
            continue
        except Exception as e:
            LOGGER.warning(f"Unexpected error terminating process {proc.pid}: {e}")
            continue
    _, alive = psutil.wait_procs(tray_processes, timeout=2)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            LOGGER.debug(f"Failed to kill process {proc.pid}: {e}")
            continue
        except Exception as e:
            LOGGER.warning(f"Unexpected error killing process {proc.pid}: {e}")
            continue
    return len(tray_processes)


def create_stop_event() -> int | None:
    handle = ctypes.windll.kernel32.CreateEventW(None, True, False, _TRAY_STOP_EVENT)
    if not handle:
        return None
    ctypes.windll.kernel32.ResetEvent(handle)
    return handle


def signal_stop_event() -> bool:
    handle = ctypes.windll.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, _TRAY_STOP_EVENT)
    if not handle:
        return False
    try:
        return bool(ctypes.windll.kernel32.SetEvent(handle))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def run_command_text(
    command: str,
    voice: Any | None = None,
    callbacks: SessionCallbacks | None = None,
    session_origin: str = "text",
    confirmation_handler: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    if callbacks:
        callbacks.on_thinking()
    try:
        normalized = normalize_transcript(command)
        effective_command = normalized.text if session_origin == "voice" else command
        if _is_self_heal_request(effective_command):
            result = _run_self_heal(voice=voice)
            _CONVERSATION.record_exchange(effective_command, result["message"], assistant_kind="self_heal")
            return result
        memory_result = _CONVERSATION.handle_memory_command(effective_command)
        if memory_result:
            should_record = not bool(memory_result.pop("skip_history_record", False))
            if should_record:
                _CONVERSATION.record_exchange(
                    effective_command,
                    str(memory_result.get("message", "")),
                    assistant_kind=str(memory_result.get("status", "memory")),
                )
            return memory_result
        corrected_command = _CONVERSATION.apply_memory_corrections(effective_command)
        if corrected_command != effective_command:
            LOGGER.info("Applied memory correction: %s -> %s", effective_command, corrected_command)
            effective_command = corrected_command
        memory_answer = _CONVERSATION.answer_from_memory(effective_command)
        if memory_answer:
            _CONVERSATION.record_exchange(effective_command, memory_answer, assistant_kind="memory_answer")
            return {"status": "answered", "message": memory_answer, "results": []}
        semantic_answer = _CONVERSATION.answer_from_semantic_memory(effective_command)
        if semantic_answer:
            _CONVERSATION.record_exchange(effective_command, semantic_answer, assistant_kind="semantic_memory")
            return {"status": "answered", "message": semantic_answer, "results": []}
        reference_resolution = _resolve_follow_up_reference(effective_command)
        if reference_resolution.get("clarification"):
            result = {
                "status": "clarification_needed",
                "message": reference_resolution["clarification"],
                "results": [],
            }
            _CONVERSATION.record_exchange(effective_command, result["message"], assistant_kind="clarification_needed")
            return result
        direct_plan = reference_resolution.get("plan")
        if isinstance(direct_plan, dict):
            result = execute_plan(
                direct_plan,
                speaker=voice,
                session_origin=session_origin,
                voice_confirmation_handler=confirmation_handler,
            )
            result["message"] = _natural_result_message(result)
            _remember_action_targets(result)
            _CONVERSATION.record_exchange(effective_command, result["message"], assistant_kind=result.get("status", "action"))
            return result
        effective_command = str(reference_resolution.get("command") or effective_command)
        context = _CONVERSATION.prepare_context(effective_command)
        recent_turns = [asdict(turn) for turn in context.recent_turns]
        intent = classify_intent(effective_command)
        if intent in {"social", "question", "conversation"}:
            answer = answer_conversation(
                effective_command,
                memory_lines=context.memory_lines,
                recent_turns=recent_turns,
            )
            _CONVERSATION.record_exchange(effective_command, answer, assistant_kind="conversation")
            return {"status": "answered", "message": answer, "results": []}
        plan = plan_command(effective_command)
        plan_resolution = _resolve_plan_references(plan)
        if plan_resolution.get("clarification"):
            result = {
                "status": "clarification_needed",
                "message": plan_resolution["clarification"],
                "results": [],
            }
            _CONVERSATION.record_exchange(effective_command, result["message"], assistant_kind="clarification_needed")
            return result
        plan = plan_resolution.get("plan") or plan
        result = execute_plan(
            plan,
            speaker=voice,
            session_origin=session_origin,
            voice_confirmation_handler=confirmation_handler,
        )
        result["message"] = _natural_result_message(result)
        _remember_action_targets(result)
        _CONVERSATION.record_exchange(effective_command, result["message"], assistant_kind=result.get("status", "action"))
        return result
    except Exception:
        if callbacks:
            callbacks.on_error("Jarvis needs attention")
        raise


def _natural_result_message(result: dict[str, Any]) -> str:
    status = str(result.get("status", "")).strip().lower()
    results = result.get("results") or []

    if status == "completed":
        messages = [str(item.get("message", "")).strip() for item in results if str(item.get("message", "")).strip()]
        if not messages:
            return "Done."
        if len(messages) == 1:
            return messages[0]
        return ". ".join(messages)

    if status == "cancelled":
        return "Okay, I stopped there."

    return str(result.get("message", "")).strip() or "Done."


def _normalized_token_text(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value.lower()).split())


def _voice_session_should_stop(command: str) -> bool:
    normalized = _normalized_token_text(command)
    if not normalized:
        return False
    if normalized in STOP_LISTENING_TOKENS:
        return True
    return any(normalized.startswith(f"{token} ") for token in STOP_LISTENING_TOKENS)


def _should_wait_for_follow_up(result: dict[str, Any]) -> bool:
    status = str(result.get("status", "")).strip().lower()
    if status == "clarification_needed":
        return True
    if status != "answered":
        return False
    message = str(result.get("message", "")).strip()
    return message.endswith("?")


def _is_self_heal_request(command: str) -> bool:
    normalized = _normalized_token_text(command)
    return any(token in normalized for token in SELF_HEAL_TOKENS)


def _run_self_heal(voice: Any | None = None) -> dict[str, Any]:
    return health_run_self_heal(voice, policy="safe_auto").to_command_result()


def _reference_match(command: str) -> tuple[str, str] | None:
    normalized = _normalized_token_text(command)
    if normalized.startswith("now "):
        normalized = normalized[4:]
    parts = normalized.split()
    if len(parts) < 2:
        return None
    verb, ref = parts[0], parts[1]
    if verb in REFERENCE_VERBS and ref in REFERENCE_WORDS:
        return verb, ref
    return None


def _reference_clarification(verb: str) -> str:
    return f"What should I {verb}? I need the app, URL, file, or command name."


def _resolve_follow_up_reference(command: str) -> dict[str, Any]:
    match = _reference_match(command)
    if not match:
        return {"command": command, "plan": None, "clarification": ""}
    verb, _ = match

    if verb in {"open", "launch", "start", "use", "focus"}:
        app = _LAST_ACTION_TARGETS.get("app")
        if app:
            resolved_verb = "open" if verb in {"launch", "start", "use"} else verb
            return {"command": f"{resolved_verb} {app}", "plan": None, "clarification": ""}
        if verb == "open" and _LAST_ACTION_TARGETS.get("url"):
            return {
                "command": command,
                "plan": {
                    "plan": [
                        {
                            "tool": "browser",
                            "operation": "open_url",
                            "args": {"url": _LAST_ACTION_TARGETS["url"]},
                            "requires_confirmation": False,
                            "reason": "Resolved follow-up reference to recent URL.",
                        }
                    ],
                    "needs_clarification": False,
                    "clarification_question": "",
                },
                "clarification": "",
            }
        if verb == "open" and _LAST_ACTION_TARGETS.get("path"):
            return {
                "command": command,
                "plan": {
                    "plan": [
                        {
                            "tool": "files",
                            "operation": "open_file",
                            "args": {"path": _LAST_ACTION_TARGETS["path"]},
                            "requires_confirmation": False,
                            "reason": "Resolved follow-up reference to recent path.",
                        }
                    ],
                    "needs_clarification": False,
                    "clarification_question": "",
                },
                "clarification": "",
            }

    if verb == "run" and _LAST_ACTION_TARGETS.get("command"):
        return {"command": f"run {_LAST_ACTION_TARGETS['command']}", "plan": None, "clarification": ""}

    if verb in {"delete", "move", "copy", "rename"} and _LAST_ACTION_TARGETS.get("path"):
        return {"command": f"{verb} {_LAST_ACTION_TARGETS['path']}", "plan": None, "clarification": ""}

    return {"command": command, "plan": None, "clarification": _reference_clarification(verb)}


def _is_reference_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return _normalized_token_text(value) in REFERENCE_WORDS


def _resolve_plan_references(plan_data: dict[str, Any]) -> dict[str, Any]:
    steps = plan_data.get("plan", [])
    if not isinstance(steps, list):
        return {"plan": plan_data, "clarification": ""}

    resolved_steps: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            resolved_steps.append(step)
            continue
        args = dict(step.get("args", {}))
        if not any(_is_reference_value(value) for value in args.values()):
            resolved_steps.append(step)
            continue

        tool = str(step.get("tool", ""))
        operation = str(step.get("operation", ""))
        replacement_step = dict(step)

        if tool == "system" and operation in {"open_app", "focus_app"}:
            app = _LAST_ACTION_TARGETS.get("app")
            if app:
                args["name"] = app
                replacement_step["args"] = args
                resolved_steps.append(replacement_step)
                continue
            url = _LAST_ACTION_TARGETS.get("url")
            if operation == "open_app" and url:
                resolved_steps.append(
                    {
                        "tool": "browser",
                        "operation": "open_url",
                        "args": {"url": url},
                        "requires_confirmation": False,
                        "reason": "Resolved planner reference to recent URL.",
                    }
                )
                continue

        if tool == "browser" and operation == "open_url" and _LAST_ACTION_TARGETS.get("url"):
            args["url"] = _LAST_ACTION_TARGETS["url"]
            replacement_step["args"] = args
            resolved_steps.append(replacement_step)
            continue

        if tool == "files" and _LAST_ACTION_TARGETS.get("path"):
            for key in ("path", "source", "destination"):
                if _is_reference_value(args.get(key)):
                    args[key] = _LAST_ACTION_TARGETS["path"]
            replacement_step["args"] = args
            resolved_steps.append(replacement_step)
            continue

        if tool == "mcp" and _LAST_ACTION_TARGETS.get("mcp_server"):
            for key in ("name", "server_name"):
                if _is_reference_value(args.get(key)):
                    args[key] = _LAST_ACTION_TARGETS["mcp_server"]
            replacement_step["args"] = args
            resolved_steps.append(replacement_step)
            continue

        if tool == "agent" and _LAST_ACTION_TARGETS.get("agent"):
            if _is_reference_value(args.get("agent_id")):
                args["agent_id"] = _LAST_ACTION_TARGETS["agent"]
            replacement_step["args"] = args
            resolved_steps.append(replacement_step)
            continue

        verb = operation.replace("_", " ") or "do"
        return {"plan": None, "clarification": _reference_clarification(verb)}

    resolved_plan = dict(plan_data)
    resolved_plan["plan"] = resolved_steps
    return {"plan": resolved_plan, "clarification": ""}


def _remember_action_targets(result: dict[str, Any]) -> None:
    if str(result.get("status", "")).lower() not in {"completed", "failed"}:
        return
    for item in result.get("results") or []:
        if not isinstance(item, dict) or item.get("ok") is False:
            continue
        step = item.get("step") or {}
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        operation = step.get("operation")
        args = step.get("args") or {}
        if tool == "system" and operation in {"open_app", "focus_app"} and args.get("name"):
            _LAST_ACTION_TARGETS["app"] = str(args["name"])
        elif tool == "browser" and operation == "open_url" and args.get("url"):
            _LAST_ACTION_TARGETS["url"] = str(args["url"])
        elif tool == "terminal" and operation == "run_terminal" and args.get("command"):
            _LAST_ACTION_TARGETS["command"] = str(args["command"])
        elif tool == "files":
            data = item.get("data") or {}
            target = data.get("path") or args.get("path") or args.get("source") or args.get("destination")
            if target:
                _LAST_ACTION_TARGETS["path"] = str(target)
        elif tool == "mcp":
            data = item.get("data") or {}
            if data.get("server_name"):
                _LAST_ACTION_TARGETS["mcp_server"] = str(data["server_name"])
        elif tool == "agent":
            data = item.get("data") or {}
            agent = data.get("agent") or {}
            if isinstance(agent, dict) and agent.get("id"):
                _LAST_ACTION_TARGETS["agent"] = str(agent["id"])


def prompt_and_run(voice: Any | None = None) -> None:
    command = input("Jarvis> ").strip()
    if not command:
        return
    result = run_command_text(command, voice=voice)
    print(result["message"])
    for item in result["results"]:
        print(f"- {item['message']}")
    if voice:
        voice.say(result["message"])


def create_icon():
    image = Image.new("RGB", (64, 64), "black")
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill="white")
    draw.text((23, 20), "J", fill="black")
    return image


class TrayAssistantRuntime:
    def __init__(self, voice: Any) -> None:
        self.voice = voice
        self.hud = AssistantHud()
        self._icon: Any | None = None
        self._shutdown = threading.Event()
        self._busy_lock = threading.Lock()
        self._assistant_busy = False
        self._deferred_wake = False
        self._stop_event_handle: int | None = None
        self._wake_restart_attempts = 0
        self._wake_restart_lock = threading.Lock()
        self.hud.set_control_center_factory(
            lambda root: ControlCenterWindow(
                root,
                self.voice,
                _CONVERSATION,
                self._action_targets_snapshot,
                _run_self_heal,
            )
        )

    def _action_targets_snapshot(self) -> dict[str, str]:
        return dict(_LAST_ACTION_TARGETS)

    def _open_control_center(self) -> None:
        self.hud.request_control_center()

    def _run_voice_command(self, source: str) -> None:
        started_at = time.perf_counter()
        try:
            self.voice.stop_wake_listener()
            self._reset_wake_restart_attempts()
            self.hud.on_wake_detected()
            turn_started_at = started_at
            first_turn = True
            while not self._shutdown.is_set():
                if source != "push_to_talk" or not first_turn:
                    if not self.voice.play_listening_cue():
                        LOGGER.warning("Listening cue unavailable.")

                capture = self.voice.capture_command(level_callback=self.hud.on_listening)
                if capture.status in {"timeout", "empty"}:
                    message = "I did not catch that. Returning to idle."
                    self.hud.on_error(message)
                    self.voice.say(message)
                    return
                if capture.status == "error":
                    raise RuntimeError(capture.error or "Voice capture failed.")

                command = capture.text.strip()
                if _voice_session_should_stop(command):
                    self.hud.on_complete("Okay, stopping.")
                    self.voice.say("Okay, stopping.")
                    return

                normalized = normalize_transcript(command)
                recognized_at = time.perf_counter()
                if normalized.changed:
                    LOGGER.info("Voice command captured: %s | normalized: %s", normalized.raw_text, normalized.text)
                else:
                    LOGGER.info("Voice command captured: %s", command)
                LOGGER.info("Voice command recognition time: %.3fs", recognized_at - turn_started_at)

                result = run_command_text(
                    command,
                    voice=self.voice,
                    callbacks=self.hud,
                    session_origin="voice",
                    confirmation_handler=self._capture_voice_confirmation,
                )
                outcome = result.get("message", "Done.")
                status = str(result.get("status", "")).lower()
                if status == "clarification_needed":
                    self.hud.set_state("confirmation", "Awaiting directive")
                elif status in {"failed", "error"}:
                    self.hud.set_state("error", "Defensive mode active")
                else:
                    self.hud.set_state("complete", "Objective complete")
                response_ready_at = time.perf_counter()
                LOGGER.info("Voice command response time: %.3fs", response_ready_at - recognized_at)
                self.hud.on_complete(outcome)
                speech_ok = self.voice.say(outcome)
                if not speech_ok:
                    LOGGER.warning("Speech playback unavailable for response.")
                if not _should_wait_for_follow_up(result):
                    LOGGER.info("Voice command total time: %.3fs", time.perf_counter() - started_at)
                    return

                turn_started_at = time.perf_counter()
                first_turn = False
        except Exception as exc:
            message = str(exc)
            LOGGER.exception("Voice command failed: %s", message)
            self.hud.on_error(message)
            if not self.voice.say(f"Voice command failed. {message}"):
                LOGGER.warning("Speech playback unavailable for error response.")
            self._schedule_wake_listener_restart(reason="voice_command_failed")
        finally:
            time.sleep(0.2)
            self.hud.on_idle()
            if settings.wake_word_enabled and not self._shutdown.is_set() and self.voice.available():
                self.voice.start_wake_listener(self._on_wake_detected, on_error=self._on_wake_error)
            self._release_busy()

    def _capture_voice_confirmation(self, prompt: str) -> str:
        self.hud.on_confirmation(prompt)
        result = self.voice.capture_confirmation(level_callback=self.hud.on_listening)
        if result.ok:
            return result.text
        if result.status == "error":
            self.hud.on_error(result.error or "Confirmation failed.")
        return ""

    def _release_busy(self) -> None:
        should_run_deferred = False
        with self._busy_lock:
            self._assistant_busy = False
            if self._deferred_wake and not self._shutdown.is_set():
                self._deferred_wake = False
                self._assistant_busy = True
                should_run_deferred = True
        if should_run_deferred:
            threading.Thread(target=self._run_voice_command, args=("deferred",), daemon=True).start()

    def activate(self, source: str) -> bool:
        if not self.voice.available():
            self.hud.set_state("error", "Voice unavailable - degraded mode")
            return False
        with self._busy_lock:
            if self._assistant_busy:
                if source == "wake_word":
                    self._deferred_wake = True
                return False
            self._assistant_busy = True
        threading.Thread(target=self._run_voice_command, args=(source,), daemon=True).start()
        return True

    def _on_wake_detected(self, transcript: str) -> None:
        del transcript
        self.activate("wake_word")

    def _on_wake_error(self, message: str) -> None:
        if not self._shutdown.is_set():
            self.hud.on_error(message)
            self._schedule_wake_listener_restart(reason=message)

    def _reset_wake_restart_attempts(self) -> None:
        with self._wake_restart_lock:
            self._wake_restart_attempts = 0

    def _schedule_wake_listener_restart(self, reason: str) -> None:
        if not settings.self_heal_enabled or self._shutdown.is_set():
            return
        with self._wake_restart_lock:
            if self._wake_restart_attempts >= settings.self_heal_max_attempts:
                LOGGER.error("Wake listener self-heal exhausted after %s attempts. Last reason: %s", self._wake_restart_attempts, reason)
                return
            self._wake_restart_attempts += 1
            attempt = self._wake_restart_attempts

        def _restart() -> None:
            delay = settings.self_heal_restart_delay_seconds * attempt
            LOGGER.warning("Wake listener self-heal attempt %s scheduled in %.1fs because: %s", attempt, delay, reason)
            time.sleep(delay)
            if self._shutdown.is_set() or self._assistant_busy or not settings.wake_word_enabled or not self.voice.available():
                return
            try:
                self.voice.stop_wake_listener()
                self.voice.start_wake_listener(self._on_wake_detected, on_error=self._on_wake_error)
                LOGGER.info("Wake listener self-heal succeeded on attempt %s.", attempt)
                self._reset_wake_restart_attempts()
            except Exception:
                LOGGER.exception("Wake listener self-heal attempt %s failed.", attempt)

        threading.Thread(target=_restart, daemon=True, name=f"jarvis-wake-self-heal-{attempt}").start()

    def _monitor_external_stop(self) -> None:
        if not self._stop_event_handle:
            return
        wait_result = ctypes.windll.kernel32.WaitForSingleObject(self._stop_event_handle, INFINITE)
        if wait_result == WAIT_OBJECT_0:
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.voice.shutdown()
        self.hud.stop()
        if keyboard:
            try:
                keyboard.remove_hotkey(settings.push_to_talk_hotkey)
            except Exception:
                pass
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        if self._stop_event_handle:
            ctypes.windll.kernel32.SetEvent(self._stop_event_handle)
            ctypes.windll.kernel32.CloseHandle(self._stop_event_handle)
            self._stop_event_handle = None

    def start(self) -> None:
        self.hud.on_idle()
        self._stop_event_handle = create_stop_event()
        if self._stop_event_handle:
            threading.Thread(target=self._monitor_external_stop, daemon=True).start()
        threading.Thread(target=self.voice.preload, daemon=True).start()

        if settings.wake_word_enabled:
            if self.voice.available():
                self.voice.start_wake_listener(self._on_wake_detected, on_error=self._on_wake_error)
            else:
                self.hud.on_error("Microphone unavailable for wake word.")

        def _speak_now(_: Any = None, __: Any = None) -> None:
            self.activate("push_to_talk")

        def _control_center(_: Any = None, __: Any = None) -> None:
            self._open_control_center()

        def _quit(icon: Any, item: Any) -> None:
            del item
            self.shutdown()
            icon.stop()

        icon = pystray.Icon(
            "jarvis",
            create_icon(),
            settings.app_name,
            menu=pystray.Menu(
                pystray.MenuItem("Control Center", _control_center),
                pystray.MenuItem("Speak Now", _speak_now),
                pystray.MenuItem("Quit", _quit),
            ),
        )
        self._icon = icon

        if keyboard:
            keyboard.add_hotkey(settings.push_to_talk_hotkey, lambda: self.activate("push_to_talk"))

        LOGGER.info("Jarvis tray mode is starting.")
        print("Jarvis tray mode is running.")
        print("Wake word listener is active by default.")
        print(f"Push-to-talk hotkey: {settings.push_to_talk_hotkey}")
        print("Use --stop to shut down the tray runtime.")

        def _setup(icon_obj: Any) -> None:
            LOGGER.info("Jarvis tray mode started.")
            icon_obj.visible = True
            try:
                icon_obj.notify("Jarvis is running in tray mode.", settings.app_name)
            except Exception:
                LOGGER.info("Tray notification is not available on this system.")

        icon.run_detached(setup=_setup)
        try:
            self.hud.start()
        except KeyboardInterrupt:
            LOGGER.info("KeyboardInterrupt received in TrayAssistantRuntime.")
            self.shutdown()
            raise


def start_tray(voice: Any) -> None:
    if not pystray or not Image:
        print("pystray/Pillow not installed. Falling back to terminal mode.")
        prompt_and_run(voice)
        return
    TrayAssistantRuntime(voice).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis local agent runtime")
    parser.add_argument("command", nargs="*", help="Command to execute directly")
    parser.add_argument("--tray", action="store_true", help="Start Jarvis in tray mode")
    parser.add_argument("--voice", action="store_true", help="Start persistent voice-first tray mode")
    parser.add_argument("--interactive", action="store_true", help="Run one terminal prompt interaction")
    parser.add_argument("--install-startup", action="store_true", help="Install Jarvis into the Windows Startup folder")
    parser.add_argument("--remove-startup", action="store_true", help="Remove Jarvis from the Windows Startup folder")
    parser.add_argument("--stop", action="store_true", help="Stop all running Jarvis tray instances")
    args = parser.parse_args()

    voice = create_voice_interface()

    try:
        if args.install_startup:
            script_path = install_startup()
            print(f"Installed startup launcher: {script_path}")
            return 0
        if args.remove_startup:
            removed = remove_startup()
            if removed:
                print(f"Removed startup launcher: {startup_script_path()}")
            else:
                print(f"No startup launcher found at: {startup_script_path()}")
            return 0
        if args.stop:
            signalled = signal_stop_event()
            if signalled:
                time.sleep(settings.stop_grace_seconds)
            stopped = stop_running_instances()
            if signalled and stopped == 0:
                print("Sent stop signal to running Jarvis tray instance.")
            else:
                print(f"Stopped {stopped} Jarvis instance(s).")
            return 0
        if args.command:
            result = run_command_text(" ".join(args.command), voice=voice)
            print(result["message"])
            for item in result["results"]:
                print(f"- {item['message']}")
            return 0
        if args.interactive:
            prompt_and_run(voice)
            return 0
        if args.voice or args.tray or not args.command:
            if not ensure_single_tray_instance():
                print("Jarvis tray is already running.")
                return 0
            start_tray(voice)
            return 0
        return 0
    except (PlannerError, ExecutionError) as exc:
        print(f"Jarvis error: {exc}")
        voice.say(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user. Shutting down...")
        return 0
    except Exception as exc:
        LOGGER.exception("Unexpected error in main.")
        print(f"An unexpected error occurred: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
