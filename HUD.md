# Jarvis HUD — Reference Document

## Overview

The Jarvis HUD is a **screen-edge overlay** rendered across all four sides of the display using four transparent, always-on-top Tkinter windows. It provides real-time visual feedback for the assistant's state without interrupting normal desktop use.

The HUD is purely cosmetic. It does not receive focus, does not intercept mouse events, and does not interfere with any application running underneath it. This is enforced at the Windows API level.

---

## Files

| File | Purpose |
|------|---------|
| `interface/overlay.py` | Main HUD class — window creation, animation loop, state API |
| `interface/theme.py` | Color tokens and per-state visual definitions (`StateVisual`) |
| `interface/overlay_constants.py` | Legacy state style map (kept for reference, not actively used) |
| `interface/control_center.py` | Separate operator UI window — not part of the HUD overlay |

---

## Architecture

```
EdgeAuraHud
├── _root           Hidden Tk root (never shown)
├── _edges[4]       One _EdgeWindow per side: top, bottom, left, right
│   ├── window      tk.Toplevel — overrideredirect, topmost, alpha=0.98
│   └── canvas      tk.Canvas — drawn each frame
├── AnimatedValue   Smooth lerp for audio_level and energy
├── ColorAnimator   Smooth lerp for accent and glow hex colors
└── _animate()      Main loop — fires every 33 ms via root.after()
```

---

## Click-Through: How It Works

The HUD must never block clicks, drags, or keyboard input directed at applications underneath it. This is achieved using the Windows Extended Window Style API.

### What is applied

```python
GWL_EXSTYLE   = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
```

Both flags are OR'd onto every edge window 250 ms after startup:

```python
style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
```

### What each flag does

**`WS_EX_LAYERED`** — Required base flag. Makes the window eligible for transparency and compositing effects.

**`WS_EX_TRANSPARENT`** — The key flag. Makes the window fully pass-through for all mouse input. Windows hit-tests this window as if it does not exist. Clicks, scroll events, and drag operations fall through to whatever is underneath.

Together: the window is visible on screen but invisible to the input system.

### Verification

`_click_through_applied` is set to `True` only if all four windows receive the flags successfully. This is logged:

```
Edge HUD click-through applied to 4/4 windows.
```

If you see `applied to 0/4` or similar, check that the process has normal desktop access (not running under a restricted session).

### Why the 250 ms delay

`winfo_id()` returns the native HWND only after the window has been mapped by the OS. The `root.after(250, self._apply_click_through)` call ensures the HWND is valid before `SetWindowLongW` is called.

---

## Window Setup

Each edge is a `tk.Toplevel` with:

```python
window.overrideredirect(True)     # No title bar, no borders
window.attributes("-topmost", True)  # Always above other windows
window.attributes("-alpha", 0.98)    # Near-opaque but compositable
window.configure(bg=EDGE_BG)         # Dark background (#0b1320)
```

`overrideredirect(True)` removes the window from the taskbar and strips all window chrome. It cannot be moved, resized, or closed by the user.

Positions are computed at runtime from screen dimensions:

| Edge | Position | Size |
|------|----------|------|
| Top | `(0, 0)` | `screen_width × EDGE_THICKNESS` |
| Bottom | `(0, screen_height − EDGE_THICKNESS)` | `screen_width × EDGE_THICKNESS` |
| Left | `(0, 0)` | `EDGE_THICKNESS × screen_height` |
| Right | `(screen_width − EDGE_THICKNESS, 0)` | `EDGE_THICKNESS × screen_height` |

Default `EDGE_THICKNESS` is **26 px** (set in `.env` via `JARVIS_HUD_EDGE_THICKNESS`).

---

## State Machine

The HUD reflects one of six states at any time. States are set by calling methods on `EdgeAuraHud` from the assistant runtime.

| State key | Trigger method | Visual label | Accent | Glow | Animation speed |
|-----------|---------------|--------------|--------|------|-----------------|
| `idle_ready` | `on_idle()` | Standby | `#59b8ff` (cyan) | `#072045` | 0.45 — slow drift |
| `listening` | `on_listening(level)` | Listening | `#30f3c3` (teal) | `#023f35` | 2.2 — fast reactive |
| `thinking` | `on_thinking()` | Analyzing | `#ffc24f` (amber) | `#3b2303` | 1.25 — scan pulse |
| `confirmation_needed` | `on_confirmation(prompt)` | Authorize | `#ff8f5f` (orange) | `#3a1308` | 1.10 — locked pulse |
| `error` | `on_error(message)` | Alert | `#ff5566` (red) | `#380008` | 2.8 — rapid strobe |
| `complete` | `on_complete(result)` | Resolved | `#b694ff` (purple) | `#1b0d3b` | 0.68 — soft fade |

There is also a generic setter:

```python
hud.set_state("thinking", text="Running tool...")
```

Accepted shorthand keys: `idle`, `listening`, `thinking`, `confirmation`, `error`, `complete`.

---

## Animation Loop

`_animate()` runs every **33 ms** (~30 fps) via `root.after()`. Each frame:

1. Reads current state, detail text, audio level, and energy from the shared lock.
2. Updates `AnimatedValue` instances (lerp toward targets).
3. Updates `ColorAnimator` (lerp accent and glow colors toward target hex values).
4. Computes `amplitude` (combination of energy and audio level).
5. Computes `baseline` (minimum brightness — state-dependent + startup pulse).
6. Advances `_phase` (the wave offset, drives the scrolling animation).
7. Calls `_draw_edge()` for each of the four canvas windows.

### Animated values

| Value | Default | Speed | Controlled by |
|-------|---------|-------|---------------|
| `_audio_level` | 0.0 | 0.20 | `on_listening()`, `set_audio_level()` |
| `_energy` | 0.42 | 0.14 | All state transitions |
| `_color_anim.accent` | idle cyan | 0.10 | State change (lerps to new accent) |
| `_color_anim.glow` | idle dark | 0.10 | State change (lerps to new glow) |

All use `_lerp(a, b, t)` — exponential smoothing — so transitions are always smooth regardless of how quickly states change.

---

## Drawing (`_draw_edge`)

Each frame, for each edge canvas:

1. **Clear** — `canvas.delete("all")` wipes the previous frame.
2. **Background fill** — solid `EDGE_BG` (`#0b1320`) rectangle.
3. **Rail fill** — a muted blend of background and glow color, covering the inner band area.
4. **Segment loop** — `SEGMENT_COUNT` (96 for top/bottom, 48 for left/right) rectangular segments are drawn. Each segment:
   - Computes its position along the edge.
   - Samples a sine wave at `_phase + position × π × 3.4` to get `wave_energy`.
   - Derives `intensity` from `wave_energy × amplitude`, floored at `baseline × 0.55`.
   - Interpolates color between rail color and accent color based on intensity.
   - Derives rectangle thickness from intensity (3.5 px to 11.5 px).
   - Draws a filled rectangle — no outlines.
5. **Text overlays** (top and bottom edges only):
   - **Top edge**: `JARVIS` label on the left, `detail_text` on the right.
   - **Bottom edge**: Audio level meter bar (shown whenever in listening state or level > 0).

---

## Configuration (`.env`)

All HUD parameters are loaded from `.env` via `system/config.py`.

| Variable | Default | Effect |
|----------|---------|--------|
| `JARVIS_HUD_EDGE_THICKNESS` | `26` | Height/width of each edge window in pixels |
| `JARVIS_HUD_EDGE_INSET` | `8` | Inner padding — keeps animation away from screen edge |
| `JARVIS_HUD_DEBUG_VISIBLE` | `false` | Set `true` to show a dark blue background, making the HUD windows visible for layout debugging |
| `JARVIS_HUD_STARTUP_PULSE_SECONDS` | `2.5` | Duration of the bright startup pulse on launch |

---

## Public API

All methods are thread-safe (protected by `threading.Lock`).

```python
hud = EdgeAuraHud()
hud.start()                          # Launches the Tk mainloop (blocking — run in thread)
hud.stop()                           # Signals the loop to exit

hud.on_idle()                        # State: idle_ready
hud.on_wake_detected()               # State: idle_ready + raised energy
hud.on_listening(level: float)       # State: listening, 0.0–1.0 audio level
hud.on_thinking()                    # State: thinking
hud.on_confirmation(prompt: str)     # State: confirmation_needed
hud.on_error(message: str)           # State: error
hud.on_complete(result: str)         # State: complete

hud.set_state(state: str, text: str) # Generic setter with shorthand keys
hud.set_audio_level(level: float)    # Update audio level without changing state

hud.set_control_center_factory(fn)   # Register a factory for the Control Center window
hud.request_control_center()         # Open the Control Center (thread-safe, deferred to main thread)
```

`AssistantHud = EdgeAuraHud` is exported as the canonical alias used by the rest of the application.

---

## Color Tokens (`theme.py`)

```python
TOKENS = {
    "bg":             "#05070d",   # Root/main background
    "panel":          "#0c111c",   # Card/panel background
    "panel_alt":      "#0a0f19",   # Sidebar / alternate panel
    "border":         "#1a2538",   # Dividers and borders
    "text_primary":   "#e6eeff",   # Main labels and values
    "text_secondary": "#7e8fae",   # Muted / secondary labels
    "accent_neutral": "#74d7ff",   # Cyan — default interactive accent
    "accent_ok":      "#3ee6b0",   # Green — success / online
    "accent_warn":    "#ffbe55",   # Amber — warning / degraded
    "accent_error":   "#ff5f6d",   # Red — error / offline
    "accent_purple":  "#ad8dff",   # Purple — agents / memory
}
```

---

## Known Limitations & Issues

### Click-through reliability

`WS_EX_TRANSPARENT` is applied 250 ms after startup. During that window, the edge windows can intercept clicks. In practice this is not noticeable but it is a real race condition. If the HUD starts and click-through is not applied (logged as `applied to 0/4`), the edge windows will block input until the process is restarted.

**Fix path**: retry `_apply_click_through` with back-off, or apply flags before `root.mainloop()` using `root.update()` to force the HWND to be created first.

### `overrideredirect` and alt-tab

On some Windows configurations, `overrideredirect(True)` windows appear in the alt-tab switcher or temporarily steal focus during creation. This is a Tkinter/Win32 limitation. The `WS_EX_TRANSPARENT` flag mitigates this but does not fully prevent it at window creation time.

### Single-monitor only

`winfo_screenwidth()` and `winfo_screenheight()` return the primary monitor dimensions. On multi-monitor setups the HUD only covers the primary screen.

### 30 fps cap

`FRAME_DELAY_MS = 33` (~30 fps). This is deliberate — the HUD is drawn on the main Tk thread, and higher frame rates would increase CPU usage on a thread that also handles all Tk events. The animation is smooth enough at 30 fps due to the lerp-based easing.

### No true transparency

The background `#0b1320` is a near-black solid color, not transparent. True per-pixel alpha (so the desktop is visible through the rail) requires `WS_EX_LAYERED` with `SetLayeredWindowAttributes` or `UpdateLayeredWindow`, which Tkinter does not natively support. To achieve true background transparency a drawing library like `pygame` or a native Win32 window would be needed.

---

## Proposed Improvements

### 1. Robust click-through with retry

```python
def _apply_click_through(self, attempt: int = 0) -> None:
    applied = 0
    for edge in self._edges:
        try:
            hwnd = edge.window.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
            )
            applied += 1
        except Exception:
            pass
    if applied < len(self._edges) and attempt < 5 and self._root:
        self._root.after(200, lambda: self._apply_click_through(attempt + 1))
```

This retries up to 5 times with 200 ms spacing, covering slow window managers.

### 2. True transparency via `SetLayeredWindowAttributes`

```python
LWA_COLORKEY = 0x00000001
COLORKEY = 0x000001  # Near-black, distinct from any rendered color

user32.SetLayeredWindowAttributes(hwnd, COLORKEY, 0, LWA_COLORKEY)
```

Paint the background with the colorkey color instead of `EDGE_BG`. Windows composites it as transparent. Requires `WS_EX_LAYERED` (already applied). Works with Tkinter canvases without external libraries.

### 3. Move click-through application earlier

```python
def _run(self) -> None:
    root = tk.Tk()
    self._root = root
    root.withdraw()
    self._create_edge_windows(root)
    root.update()                       # Force HWND creation
    self._apply_click_through()         # Apply immediately — no delay
    self._animate()
    root.mainloop()
```

`root.update()` processes all pending geometry events and ensures HWNDs exist before the style is set. Eliminates the 250 ms race window.

### 4. Multi-monitor support

```python
import ctypes

def _get_all_monitors():
    monitors = []
    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        r = lprcMonitor.contents
        monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double
    )
    ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return monitors
```

Create a set of four edge windows per monitor returned.

### 5. Increase frame rate safely

Move the draw loop off the Tk main thread using a separate canvas buffer updated via `root.after()` at 16 ms (~60 fps). The main thread only calls `canvas.create_image()` with a pre-rendered `PhotoImage`, keeping Tk event handling unblocked.
