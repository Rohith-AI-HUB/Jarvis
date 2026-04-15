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
