from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import tkinter as tk

from interface.theme import STATE_VISUALS, TOKENS
from system.config import settings

LOGGER = logging.getLogger(__name__)

BG = TOKENS["bg"]
TEXT_PRI = TOKENS["text_primary"]
TEXT_SEC = "#c7d6f5"
TRANSPARENT_KEY = "#010101"
EDGE_BG_DEBUG = "#13233d"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
LWA_COLORKEY = 0x00000001


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _lerp_color(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(_lerp(r1, r2, t))
    g = int(_lerp(g1, g2, t))
    b = int(_lerp(b1, b2, t))
    return f"#{r:02x}{g:02x}{b:02x}"


def _to_colorref(color: str) -> int:
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return r | (g << 8) | (b << 16)


class AnimatedValue:
    __slots__ = ("value", "target", "speed")

    def __init__(self, initial: float, target: float = 0.0, speed: float = 0.15) -> None:
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
        self._speed = 0.10

    def update(self, accent: str, glow: str) -> tuple[str, str]:
        self.accent = _lerp_color(self.accent, accent, self._speed)
        self.glow = _lerp_color(self.glow, glow, self._speed)
        return self.accent, self.glow


@dataclass(slots=True)
class _MonitorSpec:
    left: int
    top: int
    width: int
    height: int


@dataclass(slots=True)
class _EdgeSpec:
    side: str
    x: int
    y: int
    width: int
    height: int
    monitor_index: int = 0


@dataclass(slots=True)
class _EdgeWindow:
    window: tk.Toplevel
    canvas: tk.Canvas
    side: str
    monitor_index: int


class EdgeAuraHud:
    EDGE_THICKNESS = max(20, settings.hud_edge_thickness)
    EDGE_PADDING = 0
    EDGE_INSET = max(4, settings.hud_edge_inset)
    FRAME_DELAY_MS = 33
    GLOW_SEGMENT_COUNT = 88
    CLICK_THROUGH_MAX_RETRIES = 5
    CLICK_THROUGH_RETRY_DELAY_MS = 200

    def __init__(self) -> None:
        self._state = "idle_ready"
        self._detail_text = "Jarvis ready"
        self._running = False
        self._root: tk.Tk | None = None
        self._edges: list[_EdgeWindow] = []
        self._control_center_factory: Callable[[tk.Tk], Any] | None = None
        self._control_center: Any | None = None
        self._control_center_requested = False
        self._lock = threading.Lock()

        idle_visual = STATE_VISUALS["idle_ready"]
        self._audio_level = AnimatedValue(0.0, 0.0, 0.22)
        self._energy = AnimatedValue(0.12, 0.12, 0.12)
        self._presence = AnimatedValue(0.06, 0.06, 0.10)
        self._color_anim = ColorAnimator(idle_visual.accent, idle_visual.glow)
        self._startup_started_at = time.perf_counter()
        self._startup_duration_s = max(1.0, settings.hud_startup_pulse_seconds)
        self._phase = 0.0
        self._corner_phase = 0.0
        self._recent_audio_peak = 0.0
        self._click_through_applied = False

    @classmethod
    def edge_specs_for_screen(cls, screen_width: int, screen_height: int) -> list[_EdgeSpec]:
        return cls.edge_specs_for_monitor(0, 0, screen_width, screen_height, monitor_index=0)

    @classmethod
    def edge_specs_for_monitor(
        cls,
        left: int,
        top: int,
        screen_width: int,
        screen_height: int,
        monitor_index: int = 0,
    ) -> list[_EdgeSpec]:
        thickness = cls.EDGE_THICKNESS
        width = screen_width - cls.EDGE_PADDING * 2
        height = screen_height - cls.EDGE_PADDING * 2
        return [
            _EdgeSpec("top", left + cls.EDGE_PADDING, top + cls.EDGE_PADDING, width, thickness, monitor_index),
            _EdgeSpec(
                "bottom",
                left + cls.EDGE_PADDING,
                top + screen_height - thickness - cls.EDGE_PADDING,
                width,
                thickness,
                monitor_index,
            ),
            _EdgeSpec("left", left + cls.EDGE_PADDING, top + cls.EDGE_PADDING, thickness, height, monitor_index),
            _EdgeSpec(
                "right",
                left + screen_width - thickness - cls.EDGE_PADDING,
                top + cls.EDGE_PADDING,
                thickness,
                height,
                monitor_index,
            ),
        ]

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
            self._energy.snap(0.10)
            self._presence.snap(0.04)

    def on_wake_detected(self) -> None:
        with self._lock:
            self._state = "idle_ready"
            self._detail_text = "Wake detected"
            self._audio_level.snap(0.0)
            self._energy.snap(0.42)
            self._presence.snap(0.24)

    def on_listening(self, level: float) -> None:
        with self._lock:
            self._state = "listening"
            self._detail_text = "Listening"
            clamped = max(0.0, min(1.0, level))
            self._audio_level.snap(clamped)
            self._energy.snap(0.58 + clamped * 0.30)
            self._presence.snap(0.42 + clamped * 0.24)
            self._recent_audio_peak = max(self._recent_audio_peak * 0.90, clamped)

    def on_thinking(self) -> None:
        with self._lock:
            self._state = "thinking"
            self._detail_text = "Processing"
            self._audio_level.snap(0.0)
            self._energy.snap(0.54)
            self._presence.snap(0.34)

    def on_confirmation(self, prompt: str) -> None:
        with self._lock:
            self._state = "confirmation_needed"
            self._detail_text = (prompt.strip() or "Please confirm")[:72]
            self._audio_level.snap(0.0)
            self._energy.snap(0.66)
            self._presence.snap(0.44)

    def on_error(self, message: str) -> None:
        with self._lock:
            self._state = "error"
            self._detail_text = (message.strip() or "Something went wrong")[:72]
            self._audio_level.snap(0.0)
            self._energy.snap(0.95)
            self._presence.snap(0.62)

    def on_complete(self, result: str) -> None:
        del result
        with self._lock:
            self._state = "complete"
            self._detail_text = "Objective complete"
            self._audio_level.snap(0.0)
            self._energy.snap(0.48)
            self._presence.snap(0.28)

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
                self._detail_text = text[:72]

    def set_audio_level(self, level: float) -> None:
        with self._lock:
            clamped = max(0.0, min(1.0, level))
            self._audio_level.snap(clamped)
            self._energy.snap(max(self._energy.target, 0.30 + clamped * 0.45))
            self._presence.snap(max(self._presence.target, 0.18 + clamped * 0.28))

    def _run(self) -> None:
        try:
            root = tk.Tk()
            self._root = root
            root.withdraw()
            root.configure(bg=BG)
            self._create_edge_windows(root)
            root.update_idletasks()
            root.update()
            self._apply_click_through()
            self._animate()
            root.mainloop()
        except Exception:
            LOGGER.exception("Edge HUD runtime failed.")
        finally:
            self._running = False
            self._root = None
            self._edges = []

    def _get_monitor_specs(self, root: tk.Tk) -> list[_MonitorSpec]:
        monitors: list[_MonitorSpec] = []

        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def _callback(
            h_monitor: wintypes.HMONITOR,
            hdc_monitor: wintypes.HDC,
            rect_ptr: ctypes.POINTER(wintypes.RECT),
            dw_data: wintypes.LPARAM,
        ) -> int:
            del h_monitor, hdc_monitor, dw_data
            rect = rect_ptr.contents
            monitors.append(_MonitorSpec(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
            return 1

        try:
            success = ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(_callback), 0)
            if success and monitors:
                return monitors
        except Exception:
            LOGGER.debug("Failed to enumerate all monitors for Edge HUD.", exc_info=True)

        return [_MonitorSpec(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]

    def _create_edge_windows(self, root: tk.Tk) -> None:
        monitor_specs = self._get_monitor_specs(root)
        LOGGER.info("Edge HUD monitor count: %s", len(monitor_specs))
        for monitor_index, monitor in enumerate(monitor_specs):
            LOGGER.info(
                "Edge HUD monitor detected: index=%s geometry=%sx%s+%s+%s",
                monitor_index,
                monitor.width,
                monitor.height,
                monitor.left,
                monitor.top,
            )
            for spec in self.edge_specs_for_monitor(
                monitor.left,
                monitor.top,
                monitor.width,
                monitor.height,
                monitor_index=monitor_index,
            ):
                bg_color = EDGE_BG_DEBUG if settings.hud_debug_visible else TRANSPARENT_KEY
                window = tk.Toplevel(root)
                window.overrideredirect(True)
                window.attributes("-topmost", True)
                window.attributes("-alpha", 0.98)
                window.configure(bg=bg_color)
                window.geometry(f"{spec.width}x{spec.height}+{spec.x}+{spec.y}")
                canvas = tk.Canvas(
                    window,
                    width=spec.width,
                    height=spec.height,
                    bg=bg_color,
                    highlightthickness=0,
                    bd=0,
                )
                canvas.pack(fill="both", expand=True)
                self._edges.append(
                    _EdgeWindow(window=window, canvas=canvas, side=spec.side, monitor_index=spec.monitor_index)
                )
                LOGGER.info(
                    "Edge HUD window created: monitor=%s side=%s geometry=%sx%s+%s+%s",
                    spec.monitor_index,
                    spec.side,
                    spec.width,
                    spec.height,
                    spec.x,
                    spec.y,
                )
        LOGGER.info("Edge HUD window count: %s", len(self._edges))

    def _apply_click_through(self, attempt: int = 0) -> None:
        applied = 0
        transparent_color = _to_colorref(TRANSPARENT_KEY)
        for edge in self._edges:
            try:
                hwnd = edge.window.winfo_id()
                user32 = ctypes.windll.user32
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
                user32.SetLayeredWindowAttributes(hwnd, transparent_color, 0, LWA_COLORKEY)
                applied += 1
            except Exception:
                LOGGER.debug(
                    "Edge HUD click-through unavailable for monitor=%s side=%s.",
                    edge.monitor_index,
                    edge.side,
                    exc_info=True,
                )
        self._click_through_applied = applied == len(self._edges)
        LOGGER.info(
            "Edge HUD click-through applied to %s/%s windows (attempt %s).",
            applied,
            len(self._edges),
            attempt + 1,
        )
        if applied < len(self._edges) and attempt < self.CLICK_THROUGH_MAX_RETRIES and self._root:
            self._root.after(
                self.CLICK_THROUGH_RETRY_DELAY_MS,
                lambda: self._apply_click_through(attempt + 1),
            )

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

    def _baseline_strength(self, state: str, startup_t: float) -> float:
        base = {
            "idle_ready": 0.018,
            "listening": 0.16,
            "thinking": 0.14,
            "confirmation_needed": 0.18,
            "error": 0.24,
            "complete": 0.12,
        }.get(state, 0.02)
        startup_pulse = 0.0
        pulse_window = settings.hud_startup_pulse_seconds
        if pulse_window > 0:
            elapsed = time.perf_counter() - self._startup_started_at
            if elapsed < pulse_window:
                startup_pulse = (1.0 - (elapsed / pulse_window)) * 0.22
        if settings.hud_debug_visible:
            startup_pulse += 0.10
            base = max(base, 0.08)
        return min(1.0, base + startup_pulse + startup_t * 0.01)

    def _should_render_text(self, state: str) -> bool:
        return state in {"confirmation_needed", "error"} and bool(self._detail_text.strip())

    def _phase_for_edge(self, edge: _EdgeWindow, position: float) -> float:
        side_offset = {
            "top": 0.0,
            "right": math.pi * 0.5,
            "bottom": math.pi,
            "left": math.pi * 1.5,
        }[edge.side]
        return self._phase + self._corner_phase + side_offset + position * math.pi * 2.2

    def _animate(self) -> None:
        if not self._root:
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
            energy = self._energy.update()
            presence = self._presence.update()
            self._recent_audio_peak *= 0.96

        visual = STATE_VISUALS.get(state, STATE_VISUALS["idle_ready"])
        startup_t = max(0.0, min(1.0, (time.perf_counter() - self._startup_started_at) / self._startup_duration_s))
        accent, glow = self._color_anim.update(visual.accent, visual.glow)
        baseline = self._baseline_strength(state, startup_t)
        amplitude = baseline + presence * 0.60 + energy * 0.22 + level * 0.26
        self._phase += 0.022 + visual.speed * 0.015 + level * 0.05
        self._corner_phase += 0.008 + visual.speed * 0.006

        for edge in self._edges:
            self._draw_edge(edge, accent, glow, amplitude, baseline, level, detail, state)

        self._root.after(self.FRAME_DELAY_MS, self._animate)

    def _draw_glow_segments(
        self,
        canvas: tk.Canvas,
        edge: _EdgeWindow,
        accent: str,
        glow: str,
        width: int,
        height: int,
        inset: int,
        amplitude: float,
        baseline: float,
        state: str,
        level: float,
    ) -> None:
        segments = self.GLOW_SEGMENT_COUNT if edge.side in {"top", "bottom"} else max(48, self.GLOW_SEGMENT_COUNT // 2)
        inner_line_color = _lerp_color(glow, accent, 0.72)
        bloom_color = _lerp_color(glow, accent, 0.28)
        if edge.side in {"top", "bottom"}:
            center = inset + 2.0 if edge.side == "top" else height - inset - 2.0
            for idx in range(segments):
                position = idx / max(1, segments - 1)
                drift = self._phase_for_edge(edge, position)
                wave = 0.5 + 0.5 * math.sin(drift)
                beat = 0.5 + 0.5 * math.sin(drift * 0.48 + self._corner_phase)
                intensity = max(baseline, min(1.0, amplitude * (0.28 + wave * 0.72)))
                if state == "listening":
                    intensity = min(1.0, intensity + level * 0.24 * beat)
                bloom_height = 3.0 + intensity * 13.0
                line_height = 1.0 + intensity * 2.8
                x0 = position * width
                x1 = min(float(width), x0 + (width / segments) + 1.0)
                y0 = center - bloom_height
                y1 = center + bloom_height
                canvas.create_rectangle(x0, y0, x1, y1, fill=_lerp_color(glow, bloom_color, 0.30 + wave * 0.30), outline="")
                canvas.create_rectangle(x0, center - line_height, x1, center + line_height, fill=_lerp_color(bloom_color, inner_line_color, 0.45 + wave * 0.45), outline="")
        else:
            center = inset + 2.0 if edge.side == "left" else width - inset - 2.0
            for idx in range(segments):
                position = idx / max(1, segments - 1)
                drift = self._phase_for_edge(edge, position)
                wave = 0.5 + 0.5 * math.sin(drift)
                beat = 0.5 + 0.5 * math.sin(drift * 0.48 + self._corner_phase)
                intensity = max(baseline, min(1.0, amplitude * (0.28 + wave * 0.72)))
                if state == "listening":
                    intensity = min(1.0, intensity + level * 0.24 * beat)
                bloom_width = 3.0 + intensity * 13.0
                line_width = 1.0 + intensity * 2.8
                y0 = position * height
                y1 = min(float(height), y0 + (height / segments) + 1.0)
                x0 = center - bloom_width
                x1 = center + bloom_width
                canvas.create_rectangle(x0, y0, x1, y1, fill=_lerp_color(glow, bloom_color, 0.30 + wave * 0.30), outline="")
                canvas.create_rectangle(center - line_width, y0, center + line_width, y1, fill=_lerp_color(bloom_color, inner_line_color, 0.45 + wave * 0.45), outline="")

    def _draw_corner_bloom(self, canvas: tk.Canvas, width: int, height: int, accent: str, glow: str, baseline: float) -> None:
        corner_glow = _lerp_color(glow, accent, 0.38 + baseline * 0.30)
        spread = 10 + baseline * 30
        corners = [
            (0, 0, spread * 2, spread * 2),
            (width - spread * 2, 0, width, spread * 2),
            (0, height - spread * 2, spread * 2, height),
            (width - spread * 2, height - spread * 2, width, height),
        ]
        for x0, y0, x1, y1 in corners:
            canvas.create_oval(x0, y0, x1, y1, fill=corner_glow, outline="")

    def _draw_edge(
        self,
        edge: _EdgeWindow,
        accent: str,
        glow: str,
        amplitude: float,
        baseline: float,
        level: float,
        detail: str,
        state: str,
    ) -> None:
        canvas = edge.canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        bg_color = EDGE_BG_DEBUG if settings.hud_debug_visible else TRANSPARENT_KEY
        canvas.create_rectangle(0, 0, width, height, fill=bg_color, outline="")

        if settings.hud_debug_visible:
            debug_color = _lerp_color("#0f1a2d", accent, 0.08)
            canvas.create_rectangle(0, 0, width, height, fill=debug_color, outline="")

        inset = min(self.EDGE_INSET, max(3, min(width, height) // 2))
        self._draw_corner_bloom(canvas, width, height, accent, glow, baseline)
        self._draw_glow_segments(canvas, edge, accent, glow, width, height, inset, amplitude, baseline, state, level)

        if self._should_render_text(state) and edge.side == "top":
            canvas.create_text(
                width / 2,
                height / 2,
                anchor="center",
                text=detail[:56],
                fill=_lerp_color(TEXT_SEC, TEXT_PRI, 0.35),
                font=("Segoe UI", 10),
            )


AssistantHud = EdgeAuraHud
