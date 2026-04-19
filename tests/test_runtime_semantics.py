from __future__ import annotations

from interface.theme import STATE_VISUALS
from system.diagnostics import _planner_confidence, _threat_level


def test_planner_confidence_levels() -> None:
    assert _planner_confidence({"ok": False, "stage": "idle"}) == "low"
    assert _planner_confidence({"ok": True, "stage": "idle"}) == "high"
    assert _planner_confidence({"ok": True, "stage": "thinking"}) == "medium"


def test_threat_level_priority() -> None:
    assert _threat_level("degraded", True, True, True) == "high"
    assert _threat_level("healthy", False, True, True) == "high"
    assert _threat_level("healthy", True, False, True) == "medium"
    assert _threat_level("healthy", True, True, True) == "low"


def test_state_visuals_cover_all_runtime_states() -> None:
    for state in ("idle_ready", "listening", "thinking", "confirmation_needed", "error", "complete"):
        assert state in STATE_VISUALS


def test_edge_hud_geometry_creates_four_sides() -> None:
    from interface.overlay import EdgeAuraHud

    specs = EdgeAuraHud.edge_specs_for_screen(1920, 1080)

    assert [spec.side for spec in specs] == ["top", "bottom", "left", "right"]
    assert all(spec.width > 0 and spec.height > 0 for spec in specs)
    assert specs[0].height == EdgeAuraHud.EDGE_THICKNESS
    assert specs[2].width == EdgeAuraHud.EDGE_THICKNESS


def test_edge_hud_idle_baseline_is_low() -> None:
    from interface.overlay import EdgeAuraHud

    hud = EdgeAuraHud()
    hud._startup_started_at -= hud._startup_duration_s + 1.0

    idle = hud._baseline_strength("idle_ready", 1.0)
    listening = hud._baseline_strength("listening", 1.0)
    error = hud._baseline_strength("error", 1.0)

    assert idle < listening
    assert idle < error


def test_edge_hud_normal_mode_has_no_idle_text() -> None:
    from interface.overlay import EdgeAuraHud

    hud = EdgeAuraHud()

    assert hud._should_render_text("idle_ready") is False
    assert hud._should_render_text("listening") is False
    assert hud._should_render_text("thinking") is False
    assert hud._should_render_text("confirmation_needed") is True
    assert hud._should_render_text("error") is True


def test_hud_config_flags_parse_correctly(monkeypatch) -> None:
    import importlib
    import system.config as config_module

    monkeypatch.setenv("JARVIS_HUD_EDGE_THICKNESS", "30")
    monkeypatch.setenv("JARVIS_HUD_EDGE_INSET", "10")
    monkeypatch.setenv("JARVIS_HUD_DEBUG_VISIBLE", "true")
    monkeypatch.setenv("JARVIS_HUD_STARTUP_PULSE_SECONDS", "3.0")

    reloaded = importlib.reload(config_module)

    assert reloaded.settings.hud_edge_thickness == 30
    assert reloaded.settings.hud_edge_inset == 10
    assert reloaded.settings.hud_debug_visible is True
    assert reloaded.settings.hud_startup_pulse_seconds == 3.0
    monkeypatch.undo()
    importlib.reload(config_module)


def test_app_typing_heuristic_prefers_non_terminal_actions() -> None:
    from core.planner import _heuristic_plan

    plan = _heuristic_plan("type hello world in notepad")

    assert plan is not None
    assert [step["operation"] for step in plan["plan"]] == ["focus_app", "type_text"]
    assert all(step["tool"] != "terminal" for step in plan["plan"])


def test_app_shortcut_heuristic_prefers_non_terminal_actions() -> None:
    from core.planner import _heuristic_plan

    plan = _heuristic_plan("press control l in chrome")

    assert plan is not None
    assert [step["operation"] for step in plan["plan"]] == ["focus_app", "press_keys"]
    assert plan["plan"][1]["args"]["keys"] == ["ctrl", "l"]


def test_explicit_terminal_request_still_maps_to_run_terminal() -> None:
    from core.planner import _heuristic_plan

    plan = _heuristic_plan("run git status")

    assert plan is not None
    assert plan["plan"][0]["tool"] == "terminal"
    assert plan["plan"][0]["operation"] == "run_terminal"


def test_confirmation_new_utterance_becomes_next_voice_command() -> None:
    from core.executor import execute_plan

    plan = {
        "plan": [
            {
                "tool": "system",
                "operation": "open_app",
                "args": {"name": "notepad"},
                "requires_confirmation": True,
                "reason": "Need confirmation.",
            }
        ],
        "needs_clarification": False,
        "clarification_question": "",
    }

    result = execute_plan(
        plan,
        session_origin="voice",
        voice_confirmation_handler=lambda prompt: "open chrome",
    )

    assert result["status"] == "interrupted"
    assert result["interrupted_command"] == "open chrome"


def test_tray_runtime_interrupt_stops_speech_and_queues_barge_in(monkeypatch) -> None:
    import app

    class FakeHud:
        def set_control_center_factory(self, factory):
            self.factory = factory

        def request_control_center(self):
            return None

        def on_idle(self):
            return None

        def on_wake_detected(self):
            return None

        def on_listening(self, level: float):
            return None

        def on_thinking(self):
            return None

        def on_confirmation(self, prompt: str):
            return None

        def on_error(self, message: str):
            return None

        def on_complete(self, result: str):
            return None

        def set_state(self, state: str, text: str | None = None):
            return None

        def stop(self):
            return None

        def start(self):
            return None

    class FakeVoice:
        def __init__(self) -> None:
            self.stop_calls = 0

        def available(self) -> bool:
            return True

        def is_speaking(self) -> bool:
            return True

        def stop_speaking(self) -> bool:
            self.stop_calls += 1
            return True

    monkeypatch.setattr(app, "AssistantHud", FakeHud)

    runtime = app.TrayAssistantRuntime(FakeVoice())
    runtime._assistant_busy = True

    activated = runtime.activate("wake_word")

    assert activated is True
    assert runtime.voice.stop_calls == 1
    assert runtime._consume_barge_in() == "wake_word"
    assert runtime._consume_barge_in() is None


def test_interrupt_does_not_cancel_completed_action_result(monkeypatch) -> None:
    import app

    observed_commands: list[str] = []

    def fake_run_command_text(command: str, **kwargs):
        observed_commands.append(command)
        if len(observed_commands) == 1:
            return {"status": "completed", "message": "Opened app: notepad", "results": []}
        return {"status": "answered", "message": "Follow-up handled", "results": []}

    class FakeHud:
        def set_control_center_factory(self, factory):
            return None

        def request_control_center(self):
            return None

        def on_idle(self):
            return None

        def on_wake_detected(self):
            return None

        def on_listening(self, level: float):
            return None

        def on_thinking(self):
            return None

        def on_confirmation(self, prompt: str):
            return None

        def on_error(self, message: str):
            return None

        def on_complete(self, result: str):
            return None

        def set_state(self, state: str, text: str | None = None):
            return None

    class FakeVoice:
        def __init__(self) -> None:
            self.speaking = False
            self.played = 0
            self.stopped = 0
            self.captures = iter(["open notepad", "what is next"])

        def available(self) -> bool:
            return True

        def stop_wake_listener(self) -> None:
            return None

        def start_wake_listener(self, *args, **kwargs) -> None:
            return None

        def play_listening_cue(self) -> bool:
            return True

        def capture_command(self, level_callback=None):
            class Result:
                status = "ok"

                def __init__(self, text: str) -> None:
                    self.text = text

            return Result(next(self.captures))

        def say(self, text: str) -> bool:
            self.played += 1
            self.speaking = True
            if self.played == 1:
                runtime.activate("wake_word")
            self.speaking = False
            return False

        def is_speaking(self) -> bool:
            return self.speaking

        def stop_speaking(self) -> bool:
            self.stopped += 1
            self.speaking = False
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(app, "AssistantHud", FakeHud)
    monkeypatch.setattr(app, "run_command_text", fake_run_command_text)

    runtime = app.TrayAssistantRuntime(FakeVoice())
    runtime._assistant_busy = True
    runtime._run_voice_command("push_to_talk")

    assert observed_commands[0] == "open notepad"
    assert observed_commands[1] == "what is next"
    assert runtime.voice.stopped == 1


def test_startup_launcher_contains_delay_and_tray_command() -> None:
    import app

    launcher = app.startup_launcher_contents()

    assert "WScript.Sleep" in launcher
    assert "--tray" in launcher
    assert "pythonw.exe" in launcher or "python.exe" in launcher


def test_answer_question_falls_back_from_ollama_to_groq(monkeypatch) -> None:
    from core import planner

    monkeypatch.setattr(planner.settings, "planner_provider", "auto")
    monkeypatch.setattr(planner.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(planner, "_answer_with_ollama", lambda user_input: (_ for _ in ()).throw(planner.PlannerError("ollama down")))
    monkeypatch.setattr(planner, "_answer_with_groq", lambda user_input: "groq answer")

    answer = planner.answer_question("what is jarvis")

    assert answer == "groq answer"
    assert planner.get_planner_status()["provider"] == "groq"


def test_plan_command_falls_back_from_groq_to_ollama_when_groq_preferred(monkeypatch) -> None:
    from core import planner

    monkeypatch.setattr(planner.settings, "planner_provider", "groq")
    monkeypatch.setattr(planner.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(planner, "_heuristic_plan", lambda user_input: None)
    monkeypatch.setattr(planner, "_plan_with_groq", lambda user_input: (_ for _ in ()).throw(planner.PlannerError("groq unavailable")))
    monkeypatch.setattr(
        planner,
        "_plan_with_ollama",
        lambda user_input: {"plan": [], "needs_clarification": False, "clarification_question": ""},
    )

    plan = planner.plan_command("build a plan for this complex request")

    assert plan["needs_clarification"] is False
    assert planner.get_planner_status()["provider"] == "ollama"
