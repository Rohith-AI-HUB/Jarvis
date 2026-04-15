from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable

from interface.theme import CONTROL_CENTER_SECTIONS, TOKENS
from system.config import settings
from system.diagnostics import RuntimeSnapshot, build_runtime_snapshot


LOGGER = logging.getLogger(__name__)

C_BG = TOKENS["bg"]
C_PANEL = TOKENS["panel"]
C_PANEL_ALT = TOKENS["panel_alt"]
C_BORDER = TOKENS["border"]
C_TEXT = TOKENS["text_primary"]
C_MUTED = TOKENS["text_secondary"]
C_ACCENT = TOKENS["accent_neutral"]
C_GREEN = TOKENS["accent_ok"]
C_YELLOW = TOKENS["accent_warn"]
C_RED = TOKENS["accent_error"]
C_PURPLE = TOKENS["accent_purple"]

FONT_BRAND = ("Segoe UI Semibold", 18)
FONT_TITLE = ("Segoe UI Semibold", 22)
FONT_SUBTITLE = ("Segoe UI", 10)
FONT_HEAD = ("Segoe UI Semibold", 11)
FONT_BODY = ("Segoe UI", 10)
FONT_MONO = ("Cascadia Mono", 9)
FONT_BADGE = ("Segoe UI Semibold", 9)
FONT_METRIC = ("Segoe UI Semibold", 18)
FONT_SMALL = ("Segoe UI", 9)


def _ok_color(ok: bool | None) -> str:
    if ok is True:
        return C_GREEN
    if ok is False:
        return C_RED
    return C_MUTED


def _severity_color(sev: str) -> str:
    return {"error": C_RED, "warning": C_YELLOW, "info": C_MUTED}.get(sev, C_MUTED)


def _confidence_color(confidence: str) -> str:
    return {"high": C_GREEN, "medium": C_YELLOW, "low": C_RED}.get(confidence, C_MUTED)


def _threat_color(level: str) -> str:
    return {"low": C_GREEN, "medium": C_YELLOW, "high": C_RED}.get(level, C_MUTED)


def _format_bool(value: Any, true_text: str = "ONLINE", false_text: str = "OFFLINE") -> str:
    return true_text if bool(value) else false_text


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _destroy_children(widget: tk.Widget) -> None:
    for child in widget.winfo_children():
        child.destroy()


def _scrollable_frame(parent: tk.Widget, bg: str) -> tuple[tk.Frame, tk.Canvas]:
    outer = tk.Frame(parent, bg=bg)
    outer.pack(fill="both", expand=True)

    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0, relief="flat")
    scrollbar = tk.Scrollbar(
        outer,
        orient="vertical",
        command=canvas.yview,
        bg=bg,
        troughcolor=bg,
        activebackground=C_ACCENT,
        width=9,
    )
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    inner = tk.Frame(canvas, bg=bg)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _sync_scrollregion(_: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_width(event: tk.Event) -> None:
        canvas.itemconfigure(window_id, width=event.width)

    inner.bind("<Configure>", _sync_scrollregion)
    canvas.bind("<Configure>", _sync_width)
    canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))
    return inner, canvas


def _section_card(parent: tk.Widget, title: str, subtitle: str = "", accent: str = C_ACCENT) -> tk.Frame:
    shell = tk.Frame(parent, bg=C_BORDER, padx=1, pady=1)
    shell.pack(fill="x", pady=(0, 14))
    body = tk.Frame(shell, bg=C_PANEL, padx=16, pady=14)
    body.pack(fill="both", expand=True)
    header = tk.Frame(body, bg=C_PANEL)
    header.pack(fill="x", pady=(0, 10))
    tk.Label(header, text=title, fg=C_TEXT, bg=C_PANEL, font=FONT_HEAD).pack(anchor="w")
    if subtitle:
        tk.Label(header, text=subtitle, fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w", pady=(2, 0))
    tk.Frame(body, bg=accent, height=2).pack(fill="x", pady=(0, 12))
    return body


def _status_chip(parent: tk.Widget, text: str, color: str) -> None:
    tk.Label(parent, text=f" {text} ", fg=C_BG, bg=color, font=FONT_BADGE, padx=6, pady=2).pack(side="left", padx=(0, 6))


def _info_row(parent: tk.Widget, label: str, value: str, color: str = C_TEXT) -> None:
    row = tk.Frame(parent, bg=C_PANEL)
    row.pack(fill="x", pady=3)
    tk.Label(row, text=label, fg=C_MUTED, bg=C_PANEL, font=FONT_BODY, anchor="w").pack(side="left")
    tk.Label(row, text=value, fg=color, bg=C_PANEL, font=FONT_BODY, anchor="e").pack(side="right")


def _log_line(parent: tk.Widget, prefix: str, body: str, prefix_color: str, body_color: str = C_TEXT) -> None:
    row = tk.Frame(parent, bg=C_PANEL)
    row.pack(fill="x", pady=2)
    tk.Label(row, text=prefix, fg=prefix_color, bg=C_PANEL, font=FONT_MONO, width=14, anchor="w").pack(side="left")
    tk.Label(row, text=body, fg=body_color, bg=C_PANEL, font=FONT_BODY, wraplength=760, justify="left", anchor="w").pack(side="left", fill="x", expand=True)


@dataclass(slots=True)
class ControlActionResult:
    ok: bool
    message: str
    target: str = ""


def open_log_file(path: Path | None = None, opener: Callable[[str], object] | None = None) -> ControlActionResult:
    log_path = path or settings.logs_path
    if not log_path.exists():
        return ControlActionResult(False, f"Log file does not exist: {log_path}", str(log_path))
    return _open_path(log_path, "Opened log file.", opener)


def open_logs_folder(path: Path | None = None, opener: Callable[[str], object] | None = None) -> ControlActionResult:
    folder = path or settings.logs_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    return _open_path(folder, "Opened logs folder.", opener)


def clear_conversation_history_action(conversation: Any, confirmed: bool) -> ControlActionResult:
    if not confirmed:
        return ControlActionResult(False, "Clear history cancelled.")
    conversation.clear_history()
    return ControlActionResult(True, "Conversation history cleared.")


def run_self_heal_action(voice: Any | None, handler: Callable[[Any | None], dict[str, Any]]) -> ControlActionResult:
    result = handler(voice)
    return ControlActionResult(
        ok=str(result.get("status", "")).lower() not in {"failed", "self_heal_failed"},
        message=str(result.get("message", "")).strip() or "Self-heal completed.",
    )


def _open_path(path: Path, message: str, opener: Callable[[str], object] | None) -> ControlActionResult:
    open_target = str(path)
    open_fn = opener or getattr(os, "startfile", None)
    if not callable(open_fn):
        return ControlActionResult(False, "Opening files not supported.", open_target)
    try:
        open_fn(open_target)
        return ControlActionResult(True, message, open_target)
    except Exception as exc:
        LOGGER.exception("Failed to open %s", open_target)
        return ControlActionResult(False, f"Failed: {exc}", open_target)


class ControlCenterWindow:
    def __init__(
        self,
        root: tk.Tk,
        voice: Any | None,
        conversation: Any,
        action_targets_provider: Callable[[], dict[str, str]],
        self_heal_handler: Callable[[Any | None], dict[str, Any]],
    ) -> None:
        self._root = root
        self._voice = voice
        self._conversation = conversation
        self._action_targets_provider = action_targets_provider
        self._self_heal_handler = self_heal_handler
        self._window: tk.Toplevel | None = None
        self._status_var = tk.StringVar(master=root, value="Ready")
        self._clock_var = tk.StringVar(master=root, value="")
        self._section_var = tk.StringVar(master=root, value=CONTROL_CENTER_SECTIONS[0])
        self._busy = False
        self._snapshot: RuntimeSnapshot | None = None

        self._nav_buttons: dict[str, tk.Button] = {}
        self._content_host: tk.Frame | None = None
        self._inspector_host: tk.Frame | None = None
        self._command_entry: tk.Entry | None = None

    def show(self) -> None:
        if self._window is None or not self._window.winfo_exists():
            self._build()
        if not self._window:
            return
        self._window.deiconify()
        self._window.lift()
        self._window.focus_force()
        self.refresh()

    def refresh(self) -> None:
        self._refresh_snapshot()
        self._set_status(f"Refreshed {time.strftime('%H:%M:%S')}")

    def _build(self) -> None:
        window = tk.Toplevel(self._root)
        self._window = window
        window.title("Jarvis | Configuration Console")
        window.geometry("1360x860")
        window.minsize(1120, 720)
        window.configure(bg=C_BG)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)

        shell = tk.Frame(window, bg=C_BG)
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg=C_PANEL_ALT, width=230)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)

        main = tk.Frame(shell, bg=C_BG)
        main.pack(side="left", fill="both", expand=True)

        self._build_header(main)
        self._build_toolbar(main)

        workspace = tk.Frame(main, bg=C_BG, padx=20, pady=18)
        workspace.pack(fill="both", expand=True)

        self._content_host = tk.Frame(workspace, bg=C_BG)
        self._content_host.pack(side="left", fill="both", expand=True, padx=(0, 18))

        self._inspector_host = tk.Frame(workspace, bg=C_PANEL_ALT, width=300, padx=16, pady=16)
        self._inspector_host.pack(side="right", fill="y")
        self._inspector_host.pack_propagate(False)

        self._tick_clock()

    def _build_sidebar(self, parent: tk.Widget) -> None:
        brand = tk.Frame(parent, bg=C_PANEL_ALT, padx=18, pady=22)
        brand.pack(fill="x")
        tk.Label(brand, text="PROJECT_JARVIS", fg=C_ACCENT, bg=C_PANEL_ALT, font=FONT_BRAND).pack(anchor="w")
        tk.Label(brand, text="Configuration node - operator workspace", fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_SMALL).pack(anchor="w", pady=(4, 0))

        tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", padx=16)

        nav = tk.Frame(parent, bg=C_PANEL_ALT, padx=14, pady=18)
        nav.pack(fill="x")
        tk.Label(nav, text="WORKSPACES", fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_BADGE).pack(anchor="w", pady=(0, 12))

        labels = {
            "Status": "System Overview",
            "Systems": "Runtime Health",
            "Memory": "Memory Console",
            "Ops Log": "Operations Log",
        }
        for section in CONTROL_CENTER_SECTIONS:
            btn = tk.Button(
                nav,
                text=labels.get(section, section),
                command=lambda value=section: self._switch_section(value),
                relief="flat",
                bd=0,
                cursor="hand2",
                anchor="w",
                padx=14,
                pady=12,
                font=FONT_BODY,
                bg=C_PANEL_ALT,
                fg=C_MUTED,
                activebackground=C_PANEL,
                activeforeground=C_TEXT,
            )
            btn.pack(fill="x", pady=(0, 6))
            self._nav_buttons[section] = btn

        tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", padx=16, pady=(8, 0))

        utility = tk.Frame(parent, bg=C_PANEL_ALT, padx=16, pady=18)
        utility.pack(fill="x")
        tk.Label(utility, text="QUICK ACTIONS", fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_BADGE).pack(anchor="w", pady=(0, 10))
        for label, cmd, color in (
            ("Refresh Snapshot", self.refresh, C_ACCENT),
            ("Run Self-Heal", self._run_self_heal, C_GREEN),
            ("Open Logs Folder", self._open_logs_folder, C_TEXT),
        ):
            tk.Button(
                utility,
                text=label,
                command=cmd,
                relief="flat",
                bd=0,
                cursor="hand2",
                anchor="w",
                padx=12,
                pady=9,
                font=FONT_BODY,
                bg=C_PANEL,
                fg=color,
                activebackground=C_BORDER,
                activeforeground=color,
            ).pack(fill="x", pady=(0, 6))

        footer = tk.Frame(parent, bg=C_PANEL_ALT, padx=16, pady=18)
        footer.pack(side="bottom", fill="x")
        tk.Frame(footer, bg=C_BORDER, height=1).pack(fill="x", pady=(0, 12))
        tk.Label(footer, textvariable=self._status_var, fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_SMALL, wraplength=180, justify="left").pack(anchor="w")
        self._update_nav_styles()

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=C_BG, padx=22, pady=20)
        header.pack(fill="x")

        left = tk.Frame(header, bg=C_BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Configuration Console", fg=C_TEXT, bg=C_BG, font=FONT_TITLE).pack(anchor="w")
        tk.Label(left, text="Operational redesign for the Jarvis application shell, not the assistant persona.", fg=C_MUTED, bg=C_BG, font=FONT_SUBTITLE).pack(anchor="w", pady=(4, 0))

        right = tk.Frame(header, bg=C_BG)
        right.pack(side="right")
        status_row = tk.Frame(right, bg=C_BG)
        status_row.pack(anchor="e")
        _status_chip(status_row, "NODE ACTIVE", C_GREEN)
        _status_chip(status_row, "CONFIG APP", C_ACCENT)
        tk.Label(right, textvariable=self._clock_var, fg=C_MUTED, bg=C_BG, font=FONT_MONO).pack(anchor="e", pady=(8, 0))

    def _build_toolbar(self, parent: tk.Widget) -> None:
        toolbar = tk.Frame(parent, bg=C_PANEL, padx=22, pady=16)
        toolbar.pack(fill="x")

        command_shell = tk.Frame(toolbar, bg=C_BORDER, padx=1, pady=1)
        command_shell.pack(side="left", fill="x", expand=True)
        command_inner = tk.Frame(command_shell, bg=C_PANEL_ALT, padx=14, pady=10)
        command_inner.pack(fill="both", expand=True)
        tk.Label(command_inner, text="QUERY_SYSTEM", fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_BADGE).pack(side="left", padx=(0, 12))
        self._command_entry = tk.Entry(
            command_inner,
            bg=C_PANEL_ALT,
            fg=C_TEXT,
            insertbackground=C_ACCENT,
            relief="flat",
            bd=0,
            font=FONT_BODY,
        )
        self._command_entry.pack(side="left", fill="x", expand=True)
        self._command_entry.insert(0, "Inspect runtime health, memory state, and operational logs...")
        self._command_entry.bind("<FocusIn>", self._clear_command_prompt)

        actions = tk.Frame(toolbar, bg=C_PANEL)
        actions.pack(side="left", padx=(14, 0))
        for label, cmd, color in (
            ("Refresh", self.refresh, C_ACCENT),
            ("Self-Heal", self._run_self_heal, C_GREEN),
            ("Open Log", self._open_log_file, C_TEXT),
            ("Clear History", self._clear_history, C_RED),
        ):
            tk.Button(
                actions,
                text=label,
                command=cmd,
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=14,
                pady=10,
                font=FONT_BODY,
                bg=C_PANEL_ALT,
                fg=color,
                activebackground=C_BORDER,
                activeforeground=color,
            ).pack(side="left", padx=(0, 8))

    def _clear_command_prompt(self, _: tk.Event) -> None:
        if self._command_entry and "Inspect runtime health" in self._command_entry.get():
            self._command_entry.delete(0, "end")

    def _tick_clock(self) -> None:
        if not self._window or not self._window.winfo_exists():
            return
        self._clock_var.set(time.strftime("%Y-%m-%d  %H:%M:%S"))
        self._window.after(1000, self._tick_clock)

    def _switch_section(self, section: str) -> None:
        self._section_var.set(section)
        self._update_nav_styles()
        self._render_section()

    def _update_nav_styles(self) -> None:
        current = self._section_var.get()
        for section, btn in self._nav_buttons.items():
            active = section == current
            btn.configure(
                bg=C_PANEL if active else C_PANEL_ALT,
                fg=C_ACCENT if active else C_MUTED,
                highlightthickness=1 if active else 0,
                highlightbackground=C_BORDER,
                highlightcolor=C_BORDER,
            )

    def _refresh_snapshot(self) -> None:
        try:
            self._snapshot = build_runtime_snapshot(self._voice, self._conversation, self._action_targets_provider())
            self._render_section()
            self._render_inspector()
        except Exception as exc:
            LOGGER.exception("Control Center refresh failed.")
            self._set_status(f"Refresh failed: {exc}")

    def _render_section(self) -> None:
        if not self._content_host or not self._snapshot:
            return
        _destroy_children(self._content_host)
        section = self._section_var.get()
        if section == "Status":
            self._render_status(self._snapshot)
        elif section == "Systems":
            self._render_systems(self._snapshot)
        elif section == "Memory":
            self._render_memory(self._snapshot)
        else:
            self._render_ops_log(self._snapshot)

    def _render_inspector(self) -> None:
        if not self._inspector_host or not self._snapshot:
            return
        _destroy_children(self._inspector_host)
        snap = self._snapshot
        rt = snap.runtime
        planner = snap.planner

        tk.Label(self._inspector_host, text="Live Inspector", fg=C_TEXT, bg=C_PANEL_ALT, font=FONT_HEAD).pack(anchor="w")
        tk.Label(self._inspector_host, text="Cross-cutting status visible from every workspace.", fg=C_MUTED, bg=C_PANEL_ALT, font=FONT_SMALL, wraplength=250, justify="left").pack(anchor="w", pady=(4, 12))

        pulse = _section_card(self._inspector_host, "System Pulse", "Current runtime posture")
        status_row = tk.Frame(pulse, bg=C_PANEL)
        status_row.pack(fill="x", pady=(0, 10))
        tk.Label(status_row, text="98.4%", fg=C_TEXT, bg=C_PANEL, font=("Segoe UI Semibold", 30)).pack(side="left")
        tk.Label(status_row, text="stable flow", fg=C_GREEN if rt.get("threat_level") == "low" else C_YELLOW, bg=C_PANEL, font=FONT_BADGE).pack(side="left", padx=(10, 0), pady=(10, 0))
        _info_row(pulse, "Threat Level", str(rt.get("threat_level", "unknown")).upper(), _threat_color(str(rt.get("threat_level", ""))))
        _info_row(pulse, "Wake Listener", _format_bool(rt.get("wake_listener", {}).get("alive")))
        _info_row(pulse, "Planner Confidence", str(planner.get("confidence", "n/a")).upper(), _confidence_color(str(planner.get("confidence", ""))))
        _info_row(pulse, "Speech Backend", str(rt.get("speech_backend", "none")).upper())

        entities = _section_card(self._inspector_host, "Active Entities", "Priority components")
        for label, text, color in (
            ("Neural Engine", str(planner.get("provider", "not configured")).upper(), C_ACCENT),
            ("Telemetry", str(rt.get("latency_band", "unknown")).upper(), C_YELLOW if rt.get("latency_band") != "normal" else C_GREEN),
            ("Recovery", str(rt.get("recovery_state", "unknown")).upper(), C_PURPLE),
        ):
            row = tk.Frame(entities, bg=C_PANEL)
            row.pack(fill="x", pady=4)
            tk.Label(row, text="o", fg=color, bg=C_PANEL, font=FONT_BODY).pack(side="left", padx=(0, 8))
            tk.Label(row, text=label, fg=C_TEXT, bg=C_PANEL, font=FONT_BODY).pack(side="left")
            tk.Label(row, text=text, fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(side="right")

        memory = _section_card(self._inspector_host, "Memory Fragments", "Recent semantic anchors")
        if snap.memory:
            for item in snap.memory[:3]:
                card = tk.Frame(memory, bg=C_PANEL_ALT, padx=10, pady=9)
                card.pack(fill="x", pady=(0, 8))
                tk.Label(card, text=_truncate(item, 96), fg=C_TEXT, bg=C_PANEL_ALT, font=FONT_SMALL, wraplength=230, justify="left").pack(anchor="w")
        else:
            tk.Label(memory, text="No saved memories yet.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

    def _render_status(self, snap: RuntimeSnapshot) -> None:
        inner, _ = _scrollable_frame(self._content_host, C_BG)
        self._render_hero(inner, snap, "System Overview", "Runtime, planner, and integrations aligned in one operator surface.")

        metrics = tk.Frame(inner, bg=C_BG)
        metrics.pack(fill="x", pady=(0, 14))
        self._metric_tile(metrics, "Voice Runtime", _format_bool(snap.runtime.get("voice_available")), _ok_color(snap.runtime.get("voice_available")))
        self._metric_tile(metrics, "Threat Level", str(snap.runtime.get("threat_level", "unknown")).upper(), _threat_color(str(snap.runtime.get("threat_level", ""))))
        self._metric_tile(metrics, "MCP Servers", str(len(snap.mcp.get("servers", []))), C_ACCENT)
        self._metric_tile(metrics, "Tracked Agents", str(len(snap.agents.get("agents", []))), C_PURPLE)

        grid = tk.Frame(inner, bg=C_BG)
        grid.pack(fill="x")
        left = tk.Frame(grid, bg=C_BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = tk.Frame(grid, bg=C_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        voice = _section_card(left, "Voice Runtime", "Micro-service view of the speech stack")
        _info_row(voice, "Availability", _format_bool(snap.runtime.get("voice_available")), _ok_color(snap.runtime.get("voice_available")))
        _info_row(voice, "Wake Listener", _format_bool(snap.runtime.get("wake_listener", {}).get("alive")), _ok_color(snap.runtime.get("wake_listener", {}).get("alive")))
        _info_row(voice, "Wake Word", f"{snap.runtime.get('wake_word', '-')} / enabled={snap.runtime.get('wake_word_enabled')}")
        _info_row(voice, "Whisper", f"{snap.runtime.get('whisper_device', '?')} * {snap.runtime.get('whisper_compute_type', '?')}")
        _info_row(voice, "Speech Backend", str(snap.runtime.get("speech_backend", "none")).upper())

        planner = _section_card(left, "Planner", "Decision engine and routing confidence")
        _info_row(planner, "Status", "OK" if snap.planner.get("ok") else "DEGRADED", _ok_color(snap.planner.get("ok")))
        _info_row(planner, "Provider", str(snap.planner.get("provider", "-")))
        _info_row(planner, "Stage", str(snap.planner.get("stage", "idle")).upper())
        _info_row(planner, "Confidence", str(snap.planner.get("confidence", "n/a")).upper(), _confidence_color(str(snap.planner.get("confidence", ""))))
        if snap.planner.get("message"):
            _log_line(planner, "message", str(snap.planner["message"]), C_YELLOW, C_MUTED)

        mcp = _section_card(right, "MCP & Agents", "Connected runtime entities")
        _info_row(mcp, "Connected Servers", str(len(snap.mcp.get("servers", []))), C_ACCENT)
        _info_row(mcp, "Tracked Agents", str(len(snap.agents.get("agents", []))), C_PURPLE)
        for server in snap.mcp.get("servers", []):
            connected = server.get("connected")
            body = "connected" if connected else str(server.get("last_error", "offline"))
            _log_line(mcp, str(server.get("name", "mcp")), body, _ok_color(connected), C_MUTED if connected else C_RED)

        targets = _section_card(right, "Recent Targets", "Last referenced entities and paths")
        if snap.action_targets:
            for key, value in sorted(snap.action_targets.items()):
                _info_row(targets, key, value, C_ACCENT)
        else:
            tk.Label(targets, text="No recent action targets recorded.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

    def _render_systems(self, snap: RuntimeSnapshot) -> None:
        inner, _ = _scrollable_frame(self._content_host, C_BG)
        self._render_hero(inner, snap, "Runtime Health", "Health checks, repair state, and critical operations flow.")

        health = snap.health
        overall_ok = health.get("status") == "healthy"
        overview = _section_card(inner, "System Health", health.get("message", ""), C_GREEN if overall_ok else C_RED)
        top = tk.Frame(overview, bg=C_PANEL)
        top.pack(fill="x", pady=(0, 10))
        tk.Label(top, text="HEALTHY" if overall_ok else "DEGRADED", fg=C_GREEN if overall_ok else C_RED, bg=C_PANEL, font=("Segoe UI Semibold", 24)).pack(side="left")
        _status_chip(top, str(snap.runtime.get("threat_level", "unknown")).upper(), _threat_color(str(snap.runtime.get("threat_level", ""))))
        if snap.runtime.get("degraded_causes"):
            tk.Label(overview, text="Causes: " + ", ".join(str(item) for item in snap.runtime.get("degraded_causes", [])), fg=C_YELLOW, bg=C_PANEL, font=FONT_SMALL, wraplength=820, justify="left").pack(anchor="w")

        columns = tk.Frame(inner, bg=C_BG)
        columns.pack(fill="x")
        left = tk.Frame(columns, bg=C_BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = tk.Frame(columns, bg=C_BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        checks = _section_card(left, "Checks", "Live health probe output")
        for item in health.get("checks") or []:
            row = tk.Frame(checks, bg=C_PANEL)
            row.pack(fill="x", pady=4)
            tk.Label(row, text="o", fg=_ok_color(item.get("ok")), bg=C_PANEL, font=FONT_BODY).pack(side="left", padx=(0, 8))
            tk.Label(row, text=str(item.get("name", "")), fg=C_TEXT, bg=C_PANEL, font=FONT_BODY).pack(side="left")
            tk.Label(row, text=str(item.get("message", "")), fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL, wraplength=300, justify="left").pack(side="right")

        repairs = _section_card(left, "Repairs", "Most recent self-heal mutations", C_YELLOW)
        if health.get("repairs"):
            for repair in health.get("repairs") or []:
                line = tk.Frame(repairs, bg=C_PANEL_ALT, padx=10, pady=8)
                line.pack(fill="x", pady=(0, 8))
                tag_row = tk.Frame(line, bg=C_PANEL_ALT)
                tag_row.pack(fill="x", pady=(0, 4))
                _status_chip(tag_row, "CHANGED" if repair.get("changed") else "NO-OP", C_GREEN if repair.get("changed") else C_MUTED)
                _status_chip(tag_row, "OK" if repair.get("ok") else "FAIL", C_GREEN if repair.get("ok") else C_RED)
                tk.Label(line, text=str(repair.get("message", "")), fg=C_TEXT, bg=C_PANEL_ALT, font=FONT_SMALL, wraplength=330, justify="left").pack(anchor="w")
        else:
            tk.Label(repairs, text="No repair entries available.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

        self_heal = _section_card(right, "Recovery State", "Self-heal summary and runtime posture", C_PURPLE)
        _info_row(self_heal, "Status", str(snap.self_heal.get("status", "never_run")).upper(), C_PURPLE)
        _info_row(self_heal, "Updated", str(snap.self_heal.get("updated_at", "-")))
        _info_row(self_heal, "Recovery State", str(snap.runtime.get("recovery_state", "unknown")).upper(), C_ACCENT)
        message = str(snap.self_heal.get("message", "-"))
        tk.Label(self_heal, text=message, fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL, wraplength=360, justify="left").pack(anchor="w", pady=(8, 0))

        critical = _section_card(right, "Critical Actions", "High-signal recent operations")
        entries = snap.critical_actions or []
        if entries:
            for entry in reversed(entries[-8:]):
                _log_line(
                    critical,
                    f"{entry.get('timestamp', '--:--:--')} {str(entry.get('kind', '')).upper()}",
                    str(entry.get("message", "")),
                    _severity_color(str(entry.get("severity", "info"))),
                )
        else:
            tk.Label(critical, text="No critical actions recorded.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

    def _render_memory(self, snap: RuntimeSnapshot) -> None:
        inner, _ = _scrollable_frame(self._content_host, C_BG)
        self._render_hero(inner, snap, "Memory Console", "Saved semantic memory and recent conversational context.")

        row = tk.Frame(inner, bg=C_BG)
        row.pack(fill="x", pady=(0, 14))
        self._metric_tile(row, "Saved Memories", str(len(snap.memory)), C_ACCENT)
        self._metric_tile(row, "Recent Turns", str(len(snap.recent_history)), C_PURPLE)
        self._metric_tile(row, "Action Targets", str(len(snap.action_targets)), C_GREEN)

        memories = _section_card(inner, "Saved Memories", "Persistent memory fragments used by the assistant")
        if snap.memory:
            for item in snap.memory:
                block = tk.Frame(memories, bg=C_PANEL_ALT, padx=12, pady=10)
                block.pack(fill="x", pady=(0, 8))
                tk.Label(block, text=_truncate(item, 240), fg=C_TEXT, bg=C_PANEL_ALT, font=FONT_BODY, wraplength=820, justify="left").pack(anchor="w")
        else:
            tk.Label(memories, text="No memories saved yet.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

        history = _section_card(inner, "Recent Conversation", "Latest user and assistant exchanges", C_PURPLE)
        if snap.recent_history:
            for turn in snap.recent_history:
                row = tk.Frame(history, bg=C_PANEL, pady=4)
                row.pack(fill="x")
                role = str(turn.get("role", "?")).upper()
                role_color = C_ACCENT if role == "USER" else C_PURPLE
                tk.Label(row, text=role, fg=role_color, bg=C_PANEL, font=FONT_BADGE, width=10, anchor="w").pack(side="left")
                tk.Label(row, text=f"[{turn.get('kind', '')}]", fg=C_MUTED, bg=C_PANEL, font=FONT_BADGE, width=16, anchor="w").pack(side="left")
                tk.Label(row, text=_truncate(str(turn.get("text", "")), 160), fg=C_TEXT, bg=C_PANEL, font=FONT_BODY, anchor="w", wraplength=700, justify="left").pack(side="left", fill="x", expand=True)
        else:
            tk.Label(history, text="No recent history.", fg=C_MUTED, bg=C_PANEL, font=FONT_SMALL).pack(anchor="w")

    def _render_ops_log(self, snap: RuntimeSnapshot) -> None:
        inner, _ = _scrollable_frame(self._content_host, C_BG)
        self._render_hero(inner, snap, "Operations Log", "Recent runtime output with elevated severity scanning.")

        log_card = _section_card(inner, "Live Logs Console", "Most recent log lines from the Jarvis runtime")
        toolbar = tk.Frame(log_card, bg=C_PANEL)
        toolbar.pack(fill="x", pady=(0, 10))
        _status_chip(toolbar, "EXPORT", C_ACCENT)
        _status_chip(toolbar, "SCAN", C_GREEN)
        _status_chip(toolbar, "ALERT", C_RED if any("ERROR" in line.upper() for line in snap.recent_log_lines) else C_MUTED)

        console_shell = tk.Frame(log_card, bg=C_BORDER, padx=1, pady=1)
        console_shell.pack(fill="both", expand=True)
        console = tk.Text(
            console_shell,
            wrap="none",
            font=FONT_MONO,
            bg="#04070f",
            fg=C_TEXT,
            insertbackground=C_TEXT,
            selectbackground=C_ACCENT,
            relief="flat",
            bd=0,
            padx=12,
            pady=12,
            height=26,
        )
        console.pack(fill="both", expand=True)
        console.tag_configure("ERROR", foreground=C_RED)
        console.tag_configure("WARNING", foreground=C_YELLOW)
        console.tag_configure("DEBUG", foreground=C_MUTED)
        console.tag_configure("INFO", foreground=C_TEXT)

        for line in snap.recent_log_lines or ["No log lines available."]:
            upper = line.upper()
            tag = "INFO"
            if " ERROR " in upper or upper.startswith("ERROR"):
                tag = "ERROR"
            elif " WARNING " in upper or upper.startswith("WARNING"):
                tag = "WARNING"
            elif " DEBUG " in upper or upper.startswith("DEBUG"):
                tag = "DEBUG"
            console.insert("end", line + "\n", tag)
        console.configure(state="disabled")
        console.see("end")

    def _render_hero(self, parent: tk.Widget, snap: RuntimeSnapshot, title: str, subtitle: str) -> None:
        hero_shell = tk.Frame(parent, bg=C_BORDER, padx=1, pady=1)
        hero_shell.pack(fill="x", pady=(0, 14))
        hero = tk.Frame(hero_shell, bg=C_PANEL, padx=18, pady=16)
        hero.pack(fill="both", expand=True)

        top = tk.Frame(hero, bg=C_PANEL)
        top.pack(fill="x")
        left = tk.Frame(top, bg=C_PANEL)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, fg=C_TEXT, bg=C_PANEL, font=("Segoe UI Semibold", 20)).pack(anchor="w")
        tk.Label(left, text=subtitle, fg=C_MUTED, bg=C_PANEL, font=FONT_SUBTITLE).pack(anchor="w", pady=(4, 0))

        right = tk.Frame(top, bg=C_PANEL)
        right.pack(side="right")
        _status_chip(right, _format_bool(snap.runtime.get("voice_available")), _ok_color(snap.runtime.get("voice_available")))
        _status_chip(right, str(snap.runtime.get("threat_level", "unknown")).upper(), _threat_color(str(snap.runtime.get("threat_level", ""))))
        _status_chip(right, str(snap.planner.get("confidence", "n/a")).upper(), _confidence_color(str(snap.planner.get("confidence", ""))))

    def _metric_tile(self, parent: tk.Widget, label: str, value: str, color: str) -> None:
        shell = tk.Frame(parent, bg=C_BORDER, padx=1, pady=1)
        shell.pack(side="left", fill="x", expand=True, padx=(0, 10))
        card = tk.Frame(shell, bg=C_PANEL, padx=14, pady=12)
        card.pack(fill="both", expand=True)
        tk.Label(card, text=label, fg=C_MUTED, bg=C_PANEL, font=FONT_BADGE).pack(anchor="w")
        tk.Label(card, text=value, fg=color, bg=C_PANEL, font=FONT_METRIC).pack(anchor="w", pady=(10, 0))

    def _run_self_heal(self) -> None:
        if self._busy:
            self._set_status("Self-heal already running...")
            return
        self._busy = True
        self._set_status("Running self-heal...")

        def _work() -> None:
            result = run_self_heal_action(self._voice, self._self_heal_handler)
            self._root.after(0, lambda: self._finish_action(result, refresh=True))

        threading.Thread(target=_work, daemon=True, name="jarvis-cc-heal").start()

    def _finish_action(self, result: ControlActionResult, refresh: bool = False) -> None:
        self._busy = False
        if refresh:
            self._refresh_snapshot()
        self._set_status(result.message)

    def _open_log_file(self) -> None:
        self._set_status(open_log_file().message)

    def _open_logs_folder(self) -> None:
        self._set_status(open_logs_folder().message)

    def _clear_history(self) -> None:
        confirmed = messagebox.askyesno(
            "Clear conversation history",
            "Clear recent conversation history?\nSaved memories will be kept.",
            parent=self._window,
        )
        result = clear_conversation_history_action(self._conversation, confirmed)
        self._set_status(result.message)
        if result.ok:
            self.refresh()

    def _set_status(self, message: str) -> None:
        self._status_var.set(message[:160])
