from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Any, Callable

import tkinter as tk

from interface.theme import STATE_VISUALS, TOKENS

BG = "#010208"
TEXT_PRI = TOKENS["text_primary"]
TEXT_SEC = TOKENS["text_secondary"]
LEFT_WAVE = "#54c7ff"
RIGHT_WAVE = "#f05cff"
CORE_FILL = "#08101d"

LOGGER = logging.getLogger(__name__)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _lerp_color(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(_lerp(r1, r2, t))
    g = int(_lerp(g1, g2, t))
    b = int(_lerp(b1, b2, t))
    return f"#{r:02x}{g:02x}{b:02x}"


class AnimatedValue:
    __slots__ = ("value", "target", "speed")

    def __init__(self, initial: float, target: float = 0.0, speed: float = 0.12) -> None:
        self.value = initial
        self.target = target
        self.speed = speed

    def update(self) -> float:
        self.value = _lerp(self.value, self.target, self.speed)
        return self.value

    def snap(self, target: float) -> None:
        self.target = target


class ColorAnimator:
    __slots__ = ("accent", "glow", "_speed")

    def __init__(self, accent: str, glow: str) -> None:
        self.accent = accent
        self.glow = glow
        self._speed = 0.08

    def update(self, accent: str, glow: str) -> tuple[str, str]:
        self.accent = _lerp_color(self.accent, accent, self._speed)
        self.glow = _lerp_color(self.glow, glow, self._speed)
        return self.accent, self.glow


class OrbitParticle:
    __slots__ = ("angle", "speed", "radius", "size", "color")

    def __init__(self, angle: float, speed: float, radius: float, size: float, color: str) -> None:
        self.angle = angle
        self.speed = speed
        self.radius = radius
        self.size = size
        self.color = color

    def step(self, cx: float, cy: float, energy: float) -> tuple[float, float, float]:
        self.angle += self.speed + energy * 0.01
        radius = self.radius + energy * 18
        x = cx + math.cos(self.angle) * radius
        y = cy + math.sin(self.angle) * radius
        return x, y, self.size + energy * 1.5


class AssistantHud:
    WIN_W = 980
    WIN_H = 420
    CX = WIN_W // 2
    CY = WIN_H // 2 - 18
    ORB_R = 74
    WAVE_COUNT = 28

    def __init__(self) -> None:
        self._state = "idle_ready"
        self._detail_text = "Jarvis ready"
        self._running = False
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._control_center_factory: Callable[[tk.Tk], Any] | None = None
        self._control_center: Any | None = None
        self._control_center_requested = False
        self._lock = threading.Lock()

        idle_visual = STATE_VISUALS["idle_ready"]
        self._audio_level = AnimatedValue(0.0, 0.0, 0.2)
        self._pulse = AnimatedValue(0.0, 0.0, 0.14)
        self._orb_scale = AnimatedValue(1.0, 1.0, 0.12)
        self._color_anim = ColorAnimator(idle_visual.accent, idle_visual.glow)
        self._startup_started_at = time.perf_counter()
        self._startup_duration_s = 0.9
        self._completion_resonance_until = 0.0
        self._recent_audio_peak = 0.0
        self._phase = 0.0
        self._frame_delay_ms = 33
        self._last_frame_started = time.perf_counter()

        self._drag_x = 0
        self._drag_y = 0
        self._win_x = 0
        self._win_y = 0

        self._particles = self._init_particles()

    def _init_particles(self) -> list[OrbitParticle]:
        palette = [LEFT_WAVE, RIGHT_WAVE, "#ffffff", _lerp_color(LEFT_WAVE, RIGHT_WAVE, 0.5)]
        particles: list[OrbitParticle] = []
        for idx in range(18):
            particles.append(
                OrbitParticle(
                    angle=(2 * math.pi / 18) * idx,
                    speed=0.012 + (idx % 3) * 0.004,
                    radius=self.ORB_R + 18 + (idx % 4) * 10,
                    size=1.8 + (idx % 2),
                    color=palette[idx % len(palette)],
                )
            )
        return particles

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._startup_started_at = time.perf_counter()
        self._run()

    def stop(self) -> None:
        self._running = False

    def set_control_center_factory(self, factory: Callable[[tk.Tk], Any]) -> None:
        with self._lock:
            self._control_center_factory = factory

    def request_control_center(self) -> None:
        with self._lock:
            self._control_center_requested = True

    def on_idle(self) -> None:
        with self._lock:
            self._state = "idle_ready"
            self._detail_text = "Jarvis ready"
            self._audio_level.snap(0.0)
            self._pulse.snap(0.0)

    def on_wake_detected(self) -> None:
        with self._lock:
            self._state = "idle_ready"
            self._detail_text = "Wake detected"
            self._pulse.snap(1.0)

    def on_listening(self, level: float) -> None:
        with self._lock:
            self._state = "listening"
            self._detail_text = "Listening"
            clamped = max(0.0, min(1.0, level))
            self._audio_level.snap(clamped)
            self._recent_audio_peak = max(self._recent_audio_peak * 0.9, clamped)

    def on_thinking(self) -> None:
        with self._lock:
            self._state = "thinking"
            self._detail_text = "Processing"
            self._audio_level.snap(0.18)

    def on_confirmation(self, prompt: str) -> None:
        with self._lock:
            self._state = "confirmation_needed"
            self._detail_text = (prompt.strip() or "Please confirm")[:64]
            self._audio_level.snap(0.12)

    def on_error(self, message: str) -> None:
        with self._lock:
            self._state = "error"
            self._detail_text = (message.strip() or "Something went wrong")[:64]
            self._audio_level.snap(0.3)

    def on_complete(self, result: str) -> None:
        del result
        with self._lock:
            self._state = "complete"
            self._detail_text = "Objective complete"
            self._audio_level.snap(0.08)
            self._pulse.snap(1.0)
            self._completion_resonance_until = time.perf_counter() + 1.4

    def set_state(self, state: str, text: str | None = None) -> None:
        mapping = {
            "idle": "idle_ready",
            "listening": "listening",
            "thinking": "thinking",
            "confirmation": "confirmation_needed",
            "error": "error",
            "complete": "complete",
        }
        with self._lock:
            self._state = mapping.get(state, "idle_ready")
            if text:
                self._detail_text = text[:64]

    def set_audio_level(self, level: float) -> None:
        with self._lock:
            self._audio_level.snap(max(0.0, min(1.0, level)))

    def _run(self) -> None:
        try:
            root = tk.Tk()
            self._root = root
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.99)
            root.config(bg=BG)

            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            self._win_x = (sw - self.WIN_W) // 2
            self._win_y = (sh - self.WIN_H) // 2
            root.geometry(f"{self.WIN_W}x{self.WIN_H}+{self._win_x}+{self._win_y}")
            root.deiconify()

            self._canvas = tk.Canvas(root, width=self.WIN_W, height=self.WIN_H, bg=BG, highlightthickness=0, bd=0)
            self._canvas.pack(fill="both", expand=True)
            self._canvas.bind("<ButtonPress-1>", self._drag_start)
            self._canvas.bind("<B1-Motion>", self._drag_motion)
            self._canvas.bind("<Double-Button-1>", lambda _e: self.request_control_center())

            self._animate()
            root.mainloop()
        except Exception:
            LOGGER.exception("Overlay runtime failed.")
        finally:
            self._running = False
            self._root = None
            self._canvas = None

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root
        self._drag_y = event.y_root

    def _drag_motion(self, event: tk.Event) -> None:
        if not self._root:
            return
        dx = event.x_root - self._drag_x
        dy = event.y_root - self._drag_y
        self._drag_x = event.x_root
        self._drag_y = event.y_root
        self._win_x += dx
        self._win_y += dy
        self._root.geometry(f"+{self._win_x}+{self._win_y}")

    def _process_control_center_request(self) -> None:
        with self._lock:
            requested = self._control_center_requested
            self._control_center_requested = False
            factory = self._control_center_factory
        if not requested or not factory or not self._root:
            return
        try:
            if self._control_center is None:
                self._control_center = factory(self._root)
            self._control_center.show()
        except Exception:
            LOGGER.exception("Failed to open Control Center.")
            with self._lock:
                self._state = "error"
                self._detail_text = "Control Center failed"

    def _animate(self) -> None:
        if not self._root or not self._canvas:
            return
        if not self._running:
            try:
                self._root.quit()
                self._root.destroy()
            except Exception:
                pass
            return

        self._process_control_center_request()

        with self._lock:
            state = self._state
            detail = self._detail_text
            level = self._audio_level.update()
            self._recent_audio_peak *= 0.98

        visual = STATE_VISUALS.get(state, STATE_VISUALS["idle_ready"])
        now = time.perf_counter()
        startup_t = max(0.0, min(1.0, (now - self._startup_started_at) / self._startup_duration_s))
        cur_accent, cur_glow = self._color_anim.update(visual.accent, visual.glow)
        self._orb_scale.snap(_lerp(0.78, visual.scale, startup_t))
        scale = self._orb_scale.update()

        c = self._canvas
        c.delete("all")
        self._draw_background(c, cur_glow)
        self._draw_orb(c, cur_accent, cur_glow, scale)
        self._draw_particles(c, level)
        self._draw_waveform(c, state, level)
        self._draw_text_panel(c, cur_accent, visual.label, detail)

        frame_cost_ms = (time.perf_counter() - self._last_frame_started) * 1000.0
        self._frame_delay_ms = 40 if frame_cost_ms > 40 else 28 if frame_cost_ms < 20 else 33
        self._last_frame_started = time.perf_counter()
        self._root.after(self._frame_delay_ms, self._animate)

    def _draw_background(self, c: tk.Canvas, glow: str) -> None:
        c.create_rectangle(0, 0, self.WIN_W, self.WIN_H, fill=BG, outline="")
        for radius, color in (
            (300, _lerp_color(LEFT_WAVE, "#081525", 0.7)),
            (230, _lerp_color(RIGHT_WAVE, "#13061f", 0.7)),
            (180, glow),
        ):
            c.create_oval(self.CX - radius, self.CY - radius, self.CX + radius, self.CY + radius, fill=color, outline="")

        y = self.CY
        c.create_rectangle(0, y - 1, self.CX - self.ORB_R - 26, y + 1, fill=LEFT_WAVE, outline="")
        c.create_rectangle(self.CX + self.ORB_R + 26, y - 1, self.WIN_W, y + 1, fill=RIGHT_WAVE, outline="")

    def _draw_orb(self, c: tk.Canvas, accent: str, glow: str, scale: float) -> None:
        orb_r = self.ORB_R * scale
        pulse = self._pulse.update()
        pulse_extra = pulse * 12

        for radius, color in (
            (orb_r + 34 + pulse_extra, _lerp_color(LEFT_WAVE, RIGHT_WAVE, 0.5)),
            (orb_r + 20 + pulse_extra, glow),
            (orb_r + 8, _lerp_color(accent, "#ffffff", 0.15)),
        ):
            c.create_oval(self.CX - radius, self.CY - radius, self.CX + radius, self.CY + radius, fill=color, outline="")

        c.create_oval(self.CX - orb_r, self.CY - orb_r, self.CX + orb_r, self.CY + orb_r, fill=CORE_FILL, outline="")

        for radius, color, width in (
            (orb_r + 8, LEFT_WAVE, 3),
            (orb_r + 4, _lerp_color(LEFT_WAVE, "#ffffff", 0.35), 2),
            (orb_r + 1, _lerp_color(LEFT_WAVE, RIGHT_WAVE, 0.45), 2),
            (orb_r - 2, RIGHT_WAVE, 3),
        ):
            c.create_oval(self.CX - radius, self.CY - radius, self.CX + radius, self.CY + radius, fill="", outline=color, width=width)

        c.create_oval(
            self.CX - orb_r * 0.82,
            self.CY - orb_r * 0.82,
            self.CX + orb_r * 0.82,
            self.CY + orb_r * 0.82,
            fill="",
            outline=_lerp_color(accent, "#ffffff", 0.2),
            width=1,
        )

        core_r = orb_r * 0.54
        c.create_oval(self.CX - core_r, self.CY - core_r, self.CX + core_r, self.CY + core_r, fill="#091220", outline="")
        c.create_oval(
            self.CX - core_r * 0.2,
            self.CY - core_r * 0.2,
            self.CX + core_r * 0.2,
            self.CY + core_r * 0.2,
            fill="#ffffff",
            outline="",
        )

        reflection_y = self.CY + orb_r + 18
        for radius, color in (
            (orb_r * 1.25, _lerp_color(LEFT_WAVE, "#06111d", 0.55)),
            (orb_r * 1.45, _lerp_color(RIGHT_WAVE, "#140720", 0.55)),
        ):
            c.create_arc(
                self.CX - radius,
                reflection_y - radius * 0.24,
                self.CX + radius,
                reflection_y + radius * 0.24,
                start=0,
                extent=180,
                style="arc",
                outline=color,
                width=2,
            )

        if time.perf_counter() < self._completion_resonance_until:
            for idx in range(3):
                radius = orb_r + 24 + idx * 14 + pulse * 10
                c.create_oval(
                    self.CX - radius,
                    self.CY - radius,
                    self.CX + radius,
                    self.CY + radius,
                    fill="",
                    outline=_lerp_color(accent, "#ffffff", 0.3 + idx * 0.1),
                    width=1,
                )

    def _draw_particles(self, c: tk.Canvas, energy: float) -> None:
        self._phase += 0.02 + energy * 0.06
        for particle in self._particles:
            x, y, size = particle.step(self.CX, self.CY, energy)
            if random.random() < 0.65:
                c.create_oval(x - size, y - size, x + size, y + size, fill=particle.color, outline="")

    def _wave_height(self, idx: int, state: str, level: float) -> float:
        center_bias = 1.0 - (idx / max(1, self.WAVE_COUNT - 1)) ** 1.35
        base = 8 + center_bias * 26
        phase = self._phase * 4.5 + idx * 0.58
        wobble = (math.sin(phase) * 0.5 + 0.5) * 42

        if state == "listening":
            return base + wobble * (0.65 + level * 1.9)
        if state == "thinking":
            return base + wobble * 0.55 + 20
        if state == "confirmation_needed":
            return base + wobble * 0.4 + (8 if idx % 4 == 0 else 0)
        if state == "error":
            return base + random.random() * 54
        if state == "complete":
            return base + wobble * 0.38 + 12
        return base + wobble * (0.18 + level * 0.35)

    def _draw_waveform(self, c: tk.Canvas, state: str, level: float) -> None:
        bar_w = 4
        spacing = 7
        start_gap = self.ORB_R + 34
        center_y = self.CY

        for idx in range(self.WAVE_COUNT):
            height = self._wave_height(idx, state, level)
            distance = start_gap + idx * spacing

            left_x = self.CX - distance
            right_x = self.CX + distance - bar_w
            y0 = center_y - height / 2
            y1 = center_y + height / 2

            left_color = _lerp_color(LEFT_WAVE, "#ffffff", 0.1 + (1 - idx / self.WAVE_COUNT) * 0.25)
            right_color = _lerp_color(RIGHT_WAVE, "#ffffff", 0.1 + (1 - idx / self.WAVE_COUNT) * 0.25)

            c.create_rectangle(left_x - bar_w, y0, left_x, y1, fill=left_color, outline="")
            c.create_rectangle(right_x, y0, right_x + bar_w, y1, fill=right_color, outline="")

    def _draw_text_panel(self, c: tk.Canvas, accent: str, label: str, detail: str) -> None:
        label_y = self.CY + self.ORB_R + 70
        c.create_text(self.CX, label_y, text=label.upper(), fill=accent, font=("Segoe UI Semibold", 16), anchor="center")
        c.create_text(self.CX, label_y + 24, text=detail[:64], fill=TEXT_SEC, font=("Segoe UI", 11), anchor="center")
        c.create_text(self.CX, label_y + 48, text="Double-click to open control center", fill=_lerp_color(TEXT_SEC, accent, 0.25), font=("Segoe UI", 9), anchor="center")


BottomOverlay = AssistantHud
