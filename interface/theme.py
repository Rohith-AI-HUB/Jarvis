from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StateVisual:
    speed: float
    scale: float
    label: str
    particle_speed: float
    accent: str
    glow: str
    rail: str
    pulse: str


TOKENS = {
    "bg": "#05070d",
    "panel": "#0c111c",
    "panel_alt": "#0a0f19",
    "border": "#1a2538",
    "text_primary": "#e6eeff",
    "text_secondary": "#7e8fae",
    "accent_neutral": "#74d7ff",
    "accent_ok": "#3ee6b0",
    "accent_warn": "#ffbe55",
    "accent_error": "#ff5f6d",
    "accent_purple": "#ad8dff",
}

STATE_VISUALS: dict[str, StateVisual] = {
    "idle_ready": StateVisual(0.45, 1.00, "Standby", 0.015, "#59b8ff", "#072045", "pleasant", "core"),
    "listening": StateVisual(2.2, 1.08, "Listening", 0.034, "#30f3c3", "#023f35", "pleasant", "equalizer"),
    "thinking": StateVisual(1.25, 1.04, "Analyzing", 0.024, "#ffc24f", "#3b2303", "aggressive", "scan"),
    "confirmation_needed": StateVisual(1.10, 1.02, "Authorize", 0.021, "#ff8f5f", "#3a1308", "aggressive", "lock"),
    "error": StateVisual(2.8, 0.97, "Alert", 0.036, "#ff5566", "#380008", "aggressive", "fracture"),
    "complete": StateVisual(0.68, 1.00, "Resolved", 0.016, "#b694ff", "#1b0d3b", "pleasant", "resonance"),
}


CONTROL_CENTER_SECTIONS = ("Status", "Systems", "Memory", "Ops Log")
