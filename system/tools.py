from __future__ import annotations

import difflib
import functools
import inspect
import os
import re
import shutil
import shlex
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system.config import settings

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

try:
    import pyautogui
except ImportError:  # pragma: no cover
    pyautogui = None

try:
    import pygetwindow as gw
except ImportError:  # pragma: no cover
    gw = None

try:
    from pywinauto import Desktop
except ImportError:  # pragma: no cover
    Desktop = None


SAFE = "safe"
CONFIRM = "confirm"
BLOCKED = "blocked"


@dataclass(slots=True)
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _policy_result(message: str, *, reason: str, path: str | None = None, command: str | None = None) -> ToolResult:
    data: dict[str, Any] = {"reason": reason}
    if path:
        data["path"] = path
    if command:
        data["command"] = command
    return ToolResult(False, message, data)


def _protected_roots() -> list[Path]:
    roots: list[Path] = []
    env_roots = [
        os.environ.get("SystemRoot"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramData"),
        os.environ.get("USERPROFILE"),
    ]
    for raw in env_roots:
        if raw:
            try:
                roots.append(Path(raw).resolve())
            except Exception:
                continue
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_policy_violation(path: Path, operation: str) -> str | None:
    if path == path.anchor:
        return f"Refusing to {operation} a drive root."
    if path.parent == path:
        return f"Refusing to {operation} a filesystem root."
    user_profile = os.environ.get("USERPROFILE")
    user_profile_path = Path(user_profile).resolve() if user_profile else None
    for root in _protected_roots():
        if path == root:
            return f"Refusing to {operation} a protected root."
        if user_profile_path is not None and root == user_profile_path:
            continue
        if _is_relative_to(path, root):
            return f"Refusing to {operation} inside a protected system location."
    return None


def _enforce_path_policy(path: Path, operation: str) -> ToolResult | None:
    violation = _path_policy_violation(path, operation)
    if not violation:
        return None
    return _policy_result(violation, reason="blocked_by_policy", path=str(path))


def _command_has_shell_operators(command: str) -> bool:
    return bool(re.search(r"[;&|><`]", command))


def _safe_command_tokens(command: str) -> list[str]:
    return shlex.split(command, posix=False)


def is_terminal_command_allowed(command: str) -> tuple[bool, str]:
    normalized = command.strip()
    if not normalized:
        return False, "Command is empty."
    if _command_has_shell_operators(normalized):
        return False, "Shell chaining and redirection are blocked."
    try:
        tokens = _safe_command_tokens(normalized)
    except ValueError as exc:
        return False, f"Command could not be parsed safely: {exc}"
    if not tokens:
        return False, "Command is empty."
    for allowed in settings.allowed_terminal_commands:
        allowed_tokens = _safe_command_tokens(allowed)
        if len(tokens) >= len(allowed_tokens) and [part.lower() for part in tokens[: len(allowed_tokens)]] == [
            part.lower() for part in allowed_tokens
        ]:
            return True, ""
    return False, "Command is not in the allowlist."


def _normalize_app_name(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"\.(lnk|url|exe|appref-ms)$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@functools.lru_cache(maxsize=1)
def _start_menu_shortcuts() -> dict[str, str]:
    shortcut_map: dict[str, str] = {}
    roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.getenv("ProgramData", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".lnk", ".url", ".appref-ms"}:
                continue
            shortcut_map.setdefault(_normalize_app_name(path.stem), str(path))
    return shortcut_map


def discover_app_target(name: str) -> tuple[str | None, str | None]:
    normalized = _normalize_app_name(name)

    configured = settings.app_registry.get(normalized) or settings.app_registry.get(name.lower())
    if configured:
        expanded = os.path.expandvars(configured)
        if Path(expanded).exists() or shutil.which(expanded):
            return expanded, "configured"
        return expanded, "configured"

    for candidate in (name, f"{name}.exe", normalized.replace(" ", "")):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved, "executable"

    shortcuts = _start_menu_shortcuts()
    if normalized in shortcuts:
        return shortcuts[normalized], "shortcut"

    partial_matches = [path for key, path in shortcuts.items() if normalized and normalized in key]
    if len(partial_matches) == 1:
        return partial_matches[0], "shortcut"

    candidates = set(settings.app_registry.keys())
    candidates.update(shortcuts.keys())
    fuzzy_matches = difflib.get_close_matches(normalized, sorted(candidates), n=1, cutoff=0.72)
    if fuzzy_matches:
        match = fuzzy_matches[0]
        configured = settings.app_registry.get(match)
        if configured:
            return os.path.expandvars(configured), "configured_fuzzy"
        if match in shortcuts:
            return shortcuts[match], "shortcut_fuzzy"

    return None, None


def can_open_app(name: str) -> bool:
    target, _ = discover_app_target(name)
    return target is not None


def open_app(name: str) -> ToolResult:
    target, source = discover_app_target(name)
    if not target:
        return ToolResult(False, f"App not found: {name}")
    try:
        if source and "shortcut" in source:
            os.startfile(target)
        else:
            subprocess.Popen(target)
        return ToolResult(True, f"Opened app: {name}", {"target": target, "source": source})
    except FileNotFoundError:
        return ToolResult(False, f"App not found: {target}")
    except Exception as exc:
        return ToolResult(False, f"Failed to open app {name}: {exc}")


def focus_app(name: str) -> ToolResult:
    if gw:
        windows = gw.getWindowsWithTitle(name)
        if windows:
            window = windows[0]
            try:
                window.activate()
                return ToolResult(True, f"Focused window: {window.title}")
            except Exception:
                pass
    if Desktop:
        try:
            windows = Desktop(backend="uia").windows()
            for window in windows:
                if name.lower() in window.window_text().lower():
                    window.set_focus()
                    return ToolResult(True, f"Focused app via UI Automation: {window.window_text()}")
        except Exception:
            pass
    return ToolResult(False, f"Could not focus app: {name}")


def open_folder(path: str) -> ToolResult:
    folder = expand_path(path)
    if not folder.exists():
        return ToolResult(False, f"Folder does not exist: {folder}")
    os.startfile(folder)
    return ToolResult(True, f"Opened folder: {folder}", {"path": str(folder)})


def open_file(path: str) -> ToolResult:
    file_path = expand_path(path)
    if not file_path.exists():
        return ToolResult(False, f"File does not exist: {file_path}")
    os.startfile(file_path)
    return ToolResult(True, f"Opened file: {file_path}", {"path": str(file_path)})


def move_file(source: str, destination: str) -> ToolResult:
    src = expand_path(source)
    dst = expand_path(destination)
    if not src.exists():
        return ToolResult(False, f"Source does not exist: {src}")
    blocked = _enforce_path_policy(src, "move")
    if blocked:
        return blocked
    blocked = _enforce_path_policy(dst, "move")
    if blocked:
        return blocked
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return ToolResult(True, f"Moved {src} -> {dst}", {"source": str(src), "destination": str(dst)})


def copy_file(source: str, destination: str) -> ToolResult:
    src = expand_path(source)
    dst = expand_path(destination)
    if not src.exists():
        return ToolResult(False, f"Source does not exist: {src}")
    if src.is_dir():
        blocked = _enforce_path_policy(src, "copy")
        if blocked:
            return blocked
    blocked = _enforce_path_policy(dst, "copy")
    if blocked:
        return blocked
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return ToolResult(True, f"Copied {src} -> {dst}", {"source": str(src), "destination": str(dst)})


def rename_path(source: str, new_name: str) -> ToolResult:
    src = expand_path(source)
    if not src.exists():
        return ToolResult(False, f"Path does not exist: {src}")
    dst = src.with_name(new_name)
    blocked = _enforce_path_policy(src, "rename")
    if blocked:
        return blocked
    blocked = _enforce_path_policy(dst, "rename")
    if blocked:
        return blocked
    src.rename(dst)
    return ToolResult(True, f"Renamed {src.name} -> {dst.name}", {"source": str(src), "destination": str(dst)})


def delete_path(path: str) -> ToolResult:
    target = expand_path(path)
    if not target.exists():
        return ToolResult(False, f"Path does not exist: {target}")
    blocked = _enforce_path_policy(target, "delete")
    if blocked:
        return blocked
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return ToolResult(True, f"Deleted: {target}", {"path": str(target)})


def search_path(start: str, pattern: str) -> ToolResult:
    base = expand_path(start)
    if not base.exists():
        return ToolResult(False, f"Search base does not exist: {base}")
    matches = [str(path) for path in base.rglob("*") if pattern.lower() in path.name.lower()]
    return ToolResult(True, f"Found {len(matches)} match(es)", {"matches": matches[:50]})


def run_terminal(command: str, cwd: str | None = None) -> ToolResult:
    allowed, reason = is_terminal_command_allowed(command)
    if not allowed:
        return _policy_result(f"Command not allowed: {reason}", reason="blocked_by_policy", command=command)
    tokens = _safe_command_tokens(command.strip())
    if tokens[:1] in (["dir"], ["ls"]):
        tokens = ["powershell", "-NoProfile", "-Command", "Get-ChildItem"]
    completed = subprocess.run(
        tokens,
        shell=False,
        capture_output=True,
        text=True,
        cwd=expand_path(cwd).as_posix() if cwd else None,
    )
    ok = completed.returncode == 0
    output = (completed.stdout or completed.stderr).strip()
    return ToolResult(
        ok,
        output or f"Command exited with code {completed.returncode}",
        {"returncode": completed.returncode, "command": command},
    )


def run_workflow(name: str, cwd: str | None = None) -> ToolResult:
    command = settings.terminal_workflows.get(name)
    if not command:
        return ToolResult(False, f"Unknown workflow: {name}")
    return run_terminal(command, cwd=cwd)


def open_url(url: str) -> ToolResult:
    webbrowser.open(url)
    return ToolResult(True, f"Opened URL: {url}", {"url": url})


def click_screen(x: int, y: int) -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed for cursor fallback")
    pyautogui.click(x=x, y=y)
    return ToolResult(True, f"Clicked screen at ({x}, {y})")


def search_web(query: str, max_results: int = 5) -> ToolResult:
    try:
        import requests
        response = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_redirect": 1, "skip_disambig": 1},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        abstract = data.get("AbstractText", "")
        answer = data.get("Answer", "")
        results: list[dict[str, str]] = []
        if abstract:
            results.append({"title": "Summary", "text": abstract[:500]})
        if answer and answer != abstract:
            results.append({"title": "Answer", "text": answer[:300]})
        for topic in data.get("RelatedTopics", [])[:max_results]:
            text = topic.get("Text", "")
            if text:
                results.append({"title": topic.get("Text", "")[:60], "text": text[:200]})
        if not results:
            return ToolResult(False, f"No search results found for: {query}")
        return ToolResult(
            True,
            f"Search results for '{query}': {len(results)} found",
            {"query": query, "results": results},
        )
    except Exception as exc:
        return ToolResult(False, f"Web search failed: {exc}")


def query_playing() -> ToolResult:
    try:
        import psutil
        media_procs = ["spotify", "chrome", "msedge", "firefox", "vlc", "audacity", "music", "groove", "youtube"]
        for proc in psutil.process_iter(["name"]):
            name = proc.info.get("name", "").lower()
            if any(m in name for m in media_procs):
                return ToolResult(True, f"Media playing: {proc.info['name']}", {"playing": True, "process": proc.info["name"]})
        return ToolResult(True, "No media currently detected", {"playing": False})
    except Exception:
        return ToolResult(True, "Could not query playing media", {"playing": None})


def volume_up(amount: int = 5) -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    for _ in range(amount):
        pyautogui.press("volumeup")
    return ToolResult(True, f"Volume increased by {amount}")


def volume_down(amount: int = 5) -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    for _ in range(amount):
        pyautogui.press("volumedown")
    return ToolResult(True, f"Volume decreased by {amount}")


def media_play_pause() -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    pyautogui.press("playpause")
    return ToolResult(True, "Media play/pause toggled")


def media_next_track() -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    pyautogui.press("nexttrack")
    return ToolResult(True, "Next track")


def media_previous_track() -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    pyautogui.press("prevtrack")
    return ToolResult(True, "Previous track")


def list_processes() -> ToolResult:
    if not psutil:
        return ToolResult(False, "psutil is not installed")
    items = sorted((proc.info for proc in psutil.process_iter(["pid", "name"])), key=lambda item: item["name"] or "")
    return ToolResult(True, f"Found {len(items)} processes", {"processes": items[:100]})


def list_apps() -> ToolResult:
    names = set(settings.app_registry.keys())
    names.update(_start_menu_shortcuts().keys())
    apps = sorted(names)
    return ToolResult(True, f"Jarvis can discover {len(apps)} apps", {"apps": apps[:250], "count": len(apps)})


def list_installed_apps(save_to_path: str | None = None) -> ToolResult:
    ps_command = (
        "Get-ItemProperty "
        "HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, "
        "HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, "
        "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
        "| ForEach-Object { if ($_.DisplayName) { $_.DisplayName } } | Sort-Object -Unique"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-Command", ps_command],
            capture_output=True,
            text=True,
            check=True,
        )
        apps = [line.strip() for line in completed.stdout.strip().split("\n") if line.strip()]
        if save_to_path:
            target = expand_path(save_to_path)
            target.write_text("\n".join(apps), encoding="utf-8")
            os.startfile(target)
            return ToolResult(True, f"Saved {len(apps)} apps to {target} and opened it.", {"path": str(target), "count": len(apps)})
        display_apps = apps[:100]
        message = f"Found {len(apps)} installed apps. Here are the first {len(display_apps)}: {', '.join(display_apps)}"
        if len(apps) > 100:
            message += " ... and more."
        return ToolResult(True, message, {"all_apps": apps, "count": len(apps)})
    except Exception as exc:
        return ToolResult(False, f"Failed to list installed apps: {exc}")


def type_text(text: str, interval: float = 0.01) -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    try:
        pyautogui.write(text, interval=interval)
        return ToolResult(True, f"Typed text: {text[:50]}...")
    except Exception as exc:
        return ToolResult(False, f"Failed to type text: {exc}")


def press_keys(keys: str | list[str]) -> ToolResult:
    if not pyautogui:
        return ToolResult(False, "pyautogui is not installed")
    try:
        if isinstance(keys, str):
            pyautogui.press(keys)
        else:
            pyautogui.hotkey(*keys)
        return ToolResult(True, f"Pressed keys: {keys}")
    except Exception as exc:
        return ToolResult(False, f"Failed to press keys: {exc}")


def maximize_window(title: str) -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    windows = gw.getWindowsWithTitle(title)
    if not windows:
        return ToolResult(False, f"No window found with title: {title}")
    win = windows[0]
    try:
        win.maximize()
        return ToolResult(True, f"Maximized window: {win.title}")
    except Exception as exc:
        return ToolResult(False, f"Failed to maximize window: {exc}")


def minimize_window(title: str) -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    windows = gw.getWindowsWithTitle(title)
    if not windows:
        return ToolResult(False, f"No window found with title: {title}")
    win = windows[0]
    try:
        win.minimize()
        return ToolResult(True, f"Minimized window: {win.title}")
    except Exception as exc:
        return ToolResult(False, f"Failed to minimize window: {exc}")


def restore_window(title: str) -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    windows = gw.getWindowsWithTitle(title)
    if not windows:
        return ToolResult(False, f"No window found with title: {title}")
    win = windows[0]
    try:
        win.restore()
        return ToolResult(True, f"Restored window: {win.title}")
    except Exception as exc:
        return ToolResult(False, f"Failed to restore window: {exc}")


def tile_windows() -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    try:
        windows = [w for w in gw.getAllWindows() if w.isVisible and w.title and w.width > 100]
        if not windows:
            return ToolResult(False, "No visible windows to tile")
        import screeninfo
        monitors = screeninfo.get_monitors()
        if not monitors:
            return ToolResult(False, "No monitors detected")
        primary = monitors[0]
        cols = min(len(windows), 3)
        rows = (len(windows) + cols - 1) // cols
        win_w = primary.width // cols
        win_h = primary.height // rows
        for i, win in enumerate(windows[: cols * rows]):
            col = i % cols
            row = i // cols
            try:
                win.resizeTo(win_w, win_h)
                win.moveTo(col * win_w, row * win_h)
            except Exception:
                pass
        return ToolResult(True, f"Tiled {len(windows)} windows in {cols}x{rows} layout")
    except ImportError:
        return ToolResult(False, "screeninfo package required for tile_windows")
    except Exception as exc:
        return ToolResult(False, f"Failed to tile windows: {exc}")


def move_to_monitor(title: str, monitor: int = 1) -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    windows = gw.getWindowsWithTitle(title)
    if not windows:
        return ToolResult(False, f"No window found with title: {title}")
    win = windows[0]
    try:
        import screeninfo
        monitors = screeninfo.get_monitors()
        if monitor < 0 or monitor >= len(monitors):
            return ToolResult(False, f"Monitor {monitor} out of range (found {len(monitors)} monitors)")
        target = monitors[monitor]
        win.moveTo(target.x, target.y)
        return ToolResult(True, f"Moved {win.title} to monitor {monitor}")
    except ImportError:
        return ToolResult(False, "screeninfo package required for move_to_monitor")
    except Exception as exc:
        return ToolResult(False, f"Failed to move window: {exc}")


def close_window(title: str) -> ToolResult:
    if not gw:
        return ToolResult(False, "pygetwindow is not installed")
    windows = gw.getWindowsWithTitle(title)
    if not windows:
        return ToolResult(False, f"No window found with title: {title}")
    win = windows[0]
    try:
        win.close()
        return ToolResult(True, f"Closed window: {win.title}")
    except Exception as exc:
        return ToolResult(False, f"Failed to close window: {exc}")


def send_notification(title: str, message: str, duration: int = 5) -> ToolResult:
    try:
        from win10toast import ToastNotifier
        toaster = ToastNotifier()
        toaster.show_toast(title, message, duration=duration, threaded=False)
        return ToolResult(True, f"Notification sent: {title}")
    except ImportError:
        pass
    try:
        import win32api
        import win32con
        win32api.MessageBox(win32con.MB_OK, message, title)
        return ToolResult(True, f"Notification shown: {title}")
    except ImportError:
        pass
    try:
        import subprocess
        ps_script = f'''
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $template = @"
        <toast>
            <visual>
                <binding template="ToastText02">
                    <text id="1">{title}</text>
                    <text id="2">{message}</text>
                </binding>
            </visual>
        </toast>
        "@
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Jarvis").Show($toast)
        '''
        subprocess.run(["powershell", "-Command", ps_script], capture_output=True, timeout=15)
        return ToolResult(True, f"Notification sent: {title}")
    except Exception as exc:
        return ToolResult(False, f"Failed to send notification: {exc}")


REGISTRY: dict[str, dict[str, Any]] = {
    "system": {
        "open_app": {"handler": open_app, "risk": SAFE},
        "focus_app": {"handler": focus_app, "risk": SAFE},
        "list_apps": {"handler": list_apps, "risk": SAFE},
        "list_installed_apps": {"handler": list_installed_apps, "risk": SAFE},
        "list_processes": {"handler": list_processes, "risk": SAFE},
        "type_text": {"handler": type_text, "risk": SAFE},
        "press_keys": {"handler": press_keys, "risk": SAFE},
        "click_screen": {"handler": click_screen, "risk": CONFIRM},
    },
    "files": {
        "open_folder": {"handler": open_folder, "risk": SAFE},
        "open_file": {"handler": open_file, "risk": SAFE},
        "move_file": {"handler": move_file, "risk": CONFIRM},
        "copy_file": {"handler": copy_file, "risk": CONFIRM},
        "rename_path": {"handler": rename_path, "risk": CONFIRM},
        "delete_path": {"handler": delete_path, "risk": CONFIRM},
        "search_path": {"handler": search_path, "risk": SAFE},
    },
    "terminal": {
        "run_terminal": {"handler": run_terminal, "risk": CONFIRM},
        "run_workflow": {"handler": run_workflow, "risk": SAFE},
    },
    "browser": {
        "open_url": {"handler": open_url, "risk": SAFE},
    },
    "web": {
        "search_web": {"handler": search_web, "risk": SAFE},
    },
    "media": {
        "query_playing": {"handler": query_playing, "risk": SAFE},
        "volume_up": {"handler": volume_up, "risk": SAFE},
        "volume_down": {"handler": volume_down, "risk": SAFE},
        "media_play_pause": {"handler": media_play_pause, "risk": SAFE},
        "media_next_track": {"handler": media_next_track, "risk": SAFE},
        "media_previous_track": {"handler": media_previous_track, "risk": SAFE},
    },
    "window": {
        "maximize_window": {"handler": maximize_window, "risk": SAFE},
        "minimize_window": {"handler": minimize_window, "risk": SAFE},
        "restore_window": {"handler": restore_window, "risk": SAFE},
        "tile_windows": {"handler": tile_windows, "risk": SAFE},
        "move_to_monitor": {"handler": move_to_monitor, "risk": SAFE},
        "close_window": {"handler": close_window, "risk": CONFIRM},
    },
    "notification": {
        "send_notification": {"handler": send_notification, "risk": SAFE},
    },
}


def describe_tools() -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    for tool, operations in REGISTRY.items():
        for operation, metadata in operations.items():
            handler = metadata["handler"]
            sig = inspect.signature(handler)
            params = [
                {"name": name, "type": str(param.annotation), "required": param.default == inspect.Parameter.empty}
                for name, param in sig.parameters.items()
            ]
            described.append(
                {
                    "tool": tool,
                    "operation": operation,
                    "risk": metadata["risk"],
                    "parameters": params,
                }
            )
    return described


def resolve_handler(tool: str, operation: str) -> tuple[Any, str] | tuple[None, None]:
    tool_data = REGISTRY.get(tool)
    if not tool_data:
        return None, None
    operation_data = tool_data.get(operation)
    if not operation_data:
        return None, None
    return operation_data["handler"], operation_data["risk"]
