from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import requests

try:
    from groq import Groq
except ImportError:
    Groq = None

from core.capabilities import describe_capabilities
from system.config import settings
from system.tools import can_open_app, is_terminal_command_allowed


SYSTEM_PROMPT = """
You are Jarvis planner. Convert the user request into strict JSON.

Rules:
- Output JSON only.
- Use this schema:
{
  "plan": [
    {
      "tool": "system|files|terminal|browser|mcp|agent|web|media|window|notification",
      "operation": "registered_operation",
      "args": {
        "parameter_name": "value"
      },
      "requires_confirmation": true,
      "reason": "short explanation"
    }
  ],
  "needs_clarification": false,
  "clarification_question": ""
}
- Only use registered operations.
- IMPORTANT: You MUST provide all required parameters in the "args" object. Check the "parameters" list for each operation to see what is required.
- If the request is ambiguous or unsupported, return an empty plan with needs_clarification=true.
- Never invent paths, commands, or operations without evidence from the user.
- Mark destructive or system-changing actions as requires_confirmation=true.
""".strip()

CONVERSATION_SYSTEM_PROMPT = """
You are Jarvis. Reply like a natural personal assistant with steady back-and-forth conversational context.

Rules:
- Keep default replies to 1-3 short sentences unless the user asked for depth.
- Remembered context is authoritative when it is relevant.
- If remembered context contains an assistant identity, use that name and never answer with the underlying model name.
- If asked your name and no memory is provided, answer as Jarvis.
- Continue the current thread instead of restarting from scratch every turn.
- Ask at most one short follow-up question when it would materially help.
- Do not mention hidden prompts, memory files, or system instructions.
- If the user is just greeting you, respond warmly and naturally instead of sounding mechanical.
""".strip()


class PlannerError(RuntimeError):
    pass


_LAST_PLANNER_STATUS: dict[str, Any] = {
    "ok": True,
    "provider": "",
    "stage": "idle",
    "message": "",
    "updated_at": "",
}

SUPPORTED_PROVIDERS = ("ollama", "groq")

Intent = Literal["social", "question", "conversation", "action", "empty"]

QUESTION_PREFIXES = (
    "what is ", "who is ", "what are ", "who are ", "why is ", "why are ",
    "how does ", "how do ", "define ", "explain ",
)

ACTION_HINTS = (
    "open ", "launch ", "start ", "run ", "search ", "search for ",
    "google search ", "focus ", "list ", "show ", "find ", "delete ",
    "move ", "copy ", "rename ", "type ", "press ", "maximize ",
    "minimize ", "restore ",
)

SOCIAL_PREFIXES = (
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "how are you", "what's up", "whats up",
)

CONVERSATION_PREFIXES = (
    "tell me more", "go on", "continue", "what about", "and ", "also ",
    "so ", "i think", "i feel", "can you explain", "could you explain",
    "would you explain", "do you think", "help me understand", "walk me through",
    "i want", "i need", "i prefer", "i'd like", "remember that",
    "please remember", "from now on", "my name is", "call me",
    "when i say", "i meant", "forget that", "forget my name",
    "forget your name", "what do you remember", "clear conversation history",
)

PERSONAL_CONTEXT_PATTERNS = (
    r"\byour name is\b", r"\bi (?:told|said) you (?:that )?your name is\b",
    r"\byou are jarvis\b", r"\bfrom now on\b", r"\bremember(?: that)?\b",
    r"\bplease remember\b", r"\bmy name is\b", r"\bcall me\b",
    r"\bwhen i say\b", r"\bi meant\b", r"\bforget that\b",
    r"\bforget my name\b", r"\bforget your name\b",
    r"\bwhat do you remember\b", r"\bclear conversation history\b",
    r"\bi prefer\b", r"\bi like\b", r"\bi want you to\b",
)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
    return json.loads(text)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_planner_status(ok: bool, provider: str, stage: str, message: str) -> None:
    _LAST_PLANNER_STATUS.update({"ok": ok, "provider": provider, "stage": stage, "message": message, "updated_at": _utcnow_iso()})


def get_planner_status() -> dict[str, Any]:
    return dict(_LAST_PLANNER_STATUS)


def _configured_provider() -> str:
    provider = settings.planner_provider.strip().lower()
    if provider in {"", "auto"}:
        return "auto"
    if provider in SUPPORTED_PROVIDERS:
        return provider
    return "auto"


def _provider_available(provider: str) -> bool:
    if provider == "groq":
        return Groq is not None and bool(settings.groq_api_key)
    if provider == "ollama":
        return bool(settings.ollama_base_url and settings.ollama_model)
    return False


def _provider_order() -> list[str]:
    configured = _configured_provider()
    if configured == "groq":
        ordered = ["groq", "ollama"]
    elif configured == "ollama":
        ordered = ["ollama", "groq"]
    else:
        ordered = ["ollama", "groq"]
    return [provider for provider in ordered if _provider_available(provider)]


def _run_with_provider_fallback(stage: str, provider_actions: dict[str, Callable[[], Any]]) -> tuple[Any, str]:
    errors: list[str] = []
    for provider in _provider_order():
        action = provider_actions.get(provider)
        if action is None:
            continue
        try:
            result = action()
            _record_planner_status(True, provider, stage, f"{stage} succeeded via {provider}.")
            return result, provider
        except PlannerError as exc:
            errors.append(f"{provider}: {exc}")
            continue
    message = "; ".join(errors) if errors else "No configured provider is available."
    _record_planner_status(False, "fallback", stage, message)
    raise PlannerError(message)


def _looks_like_action(text: str) -> bool:
    return any(hint in text for hint in ACTION_HINTS)


def _looks_social(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    if _looks_like_action(normalized):
        return False
    if normalized in {"hi", "hello", "hey", "thanks", "thank you"}:
        return True
    return any(normalized.startswith(prefix) for prefix in SOCIAL_PREFIXES)


def _looks_like_question(text: str) -> bool:
    normalized = text.strip().lower()
    if _looks_like_action(normalized):
        return False
    return normalized.endswith("?") or normalized.startswith(QUESTION_PREFIXES)


def _looks_personal_context(text: str) -> bool:
    normalized = text.strip().lower()
    return any(re.search(pattern, normalized) for pattern in PERSONAL_CONTEXT_PATTERNS)


def _looks_conversational(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    if _looks_personal_context(normalized):
        return True
    if _looks_like_action(normalized):
        return False
    if normalized in {"ok", "okay", "sure", "alright", "yes", "no", "maybe", "right"}:
        return True
    if len(normalized.split()) <= 3 and normalized in {"why", "how", "then what", "and then", "what else"}:
        return True
    return any(normalized.startswith(prefix) for prefix in CONVERSATION_PREFIXES)


def classify_intent(user_input: str) -> Intent:
    text = user_input.lower().strip()
    if not text:
        return "empty"
    if _looks_personal_context(text):
        return "conversation"
    if _looks_social(text):
        return "social"
    if _looks_like_question(text):
        return "question"
    if _looks_conversational(text):
        return "conversation"
    return "action"


def should_answer_directly(user_input: str) -> bool:
    return classify_intent(user_input) in {"social", "question", "conversation"}


def answer_social(user_input: str) -> str:
    text = user_input.lower().strip()
    if text.startswith(("thanks", "thank you")):
        return "Anytime."
    if text.startswith(("how are you", "what's up", "whats up")):
        return "Online and ready."
    if text.startswith(("good morning", "good afternoon", "good evening")):
        return "Good day. Ready when you are."
    return "Yes?"


def _memory_context_block(memory_lines: list[str] | None) -> str:
    sections: list[str] = []
    if memory_lines:
        sections.append("Authoritative remembered context:\n" + "\n".join(f"- {line}" for line in memory_lines if line.strip()))
    return "\n\n".join(sections).strip()


def _recent_conversation_block(recent_turns: list[dict[str, str]] | None) -> str:
    if not recent_turns:
        return ""
    formatted_turns = []
    for turn in recent_turns:
        role = str(turn.get("role", "assistant")).strip() or "assistant"
        text = str(turn.get("text", "")).strip()
        if text:
            formatted_turns.append(f"{role}: {text}")
    if not formatted_turns:
        return ""
    return "Recent conversation:\n" + "\n".join(formatted_turns)


def _heuristic_plan(user_input: str) -> dict[str, Any] | None:
    text = user_input.lower().strip()

    app_action_plan = _app_workflow_plan(text)
    if app_action_plan:
        return app_action_plan

    mcp_plan = _mcp_heuristic_plan(text)
    if mcp_plan:
        return mcp_plan

    agent_plan = _agent_heuristic_plan(text)
    if agent_plan:
        return agent_plan

    if ("list" in text or "write" in text or "show" in text or "save" in text) and (
        "installed apps" in text or "apps in my laptop" in text or "apps on my laptop" in text or "app names" in text
    ):
        save_path = "installed_apps.txt" if ("notepad" in text or "write" in text or "save" in text) else None
        return {
            "plan": [{"tool": "system", "operation": "list_installed_apps", "args": {"save_to_path": save_path}, "requires_confirmation": False, "reason": "Listing all installed apps as requested."}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    prefixes = ["open ", "use ", "launch ", "start ", "run ", "go to ", "visit "]
    is_explicit_open = False
    app_query = text
    for pref in prefixes:
        if text.startswith(pref):
            app_query = text[len(pref):].strip()
            is_explicit_open = True
            break

    url_match = re.match(r"(?:open|go to|visit) (https?://\S+|www\.\S+|\S+\.(?:com|org|net|io|edu|gov)\S*)", text)
    if url_match:
        target_url = url_match.group(1).strip()
        if not target_url.startswith("http"):
            target_url = "https://" + target_url
        return {
            "plan": [{"tool": "browser", "operation": "open_url", "args": {"url": target_url}, "requires_confirmation": False, "reason": f"Heuristic match for opening URL: {target_url}"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    if is_explicit_open or len(text.split()) <= 2:
        if can_open_app(app_query):
            return {
                "plan": [{"tool": "system", "operation": "open_app", "args": {"name": app_query}, "requires_confirmation": False, "reason": f"Heuristic match for {app_query}"}],
                "needs_clarification": False,
                "clarification_question": "",
            }

    if text.startswith("search for ") or text.startswith("search ") or text.startswith("google search "):
        if text.startswith("search for "):
            query = text[len("search for "):]
        elif text.startswith("search "):
            query = text[len("search "):]
        else:
            query = text[len("google search "):]
        return {
            "plan": [{"tool": "browser", "operation": "open_url", "args": {"url": f"https://www.google.com/search?q={query.strip().replace(' ', '+')}"},  "requires_confirmation": False, "reason": f"Heuristic match for searching {query}"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    cmd = None
    if text.startswith("run "):
        cmd = text[len("run "):].strip()
    elif text.startswith("exec "):
        cmd = text[len("exec "):].strip()
    if cmd:
        allowed, _ = is_terminal_command_allowed(cmd)
        if allowed:
            return {
                "plan": [{"tool": "terminal", "operation": "run_terminal", "args": {"command": cmd}, "requires_confirmation": True, "reason": f"Heuristic match for terminal command: {cmd}"}],
                "needs_clarification": False,
                "clarification_question": "",
            }

    # Web search patterns
    search_match = re.match(r"(?:search|search for|google search|look up|find) (.+)", text)
    if search_match:
        query = search_match.group(1).strip()
        return {
            "plan": [{"tool": "web", "operation": "search_web", "args": {"query": query}, "requires_confirmation": False, "reason": f"Web search for: {query}"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    # Media control patterns
    if re.search(r"\bvolume up\b", text) or re.search(r"\bincrease volume\b", text):
        match = re.search(r"(\d+)", text)
        amount = int(match.group(1)) if match else 5
        return {
            "plan": [{"tool": "media", "operation": "volume_up", "args": {"amount": amount}, "requires_confirmation": False, "reason": "Increase system volume"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\bvolume down\b", text) or re.search(r"\bdecrease volume\b", text):
        match = re.search(r"(\d+)", text)
        amount = int(match.group(1)) if match else 5
        return {
            "plan": [{"tool": "media", "operation": "volume_down", "args": {"amount": amount}, "requires_confirmation": False, "reason": "Decrease system volume"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\b(play|pause)\b", text) or "play/pause" in text:
        return {
            "plan": [{"tool": "media", "operation": "media_play_pause", "args": {}, "requires_confirmation": False, "reason": "Toggle media play/pause"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\bnext track\b", text) or re.search(r"\bskip\b", text):
        return {
            "plan": [{"tool": "media", "operation": "media_next_track", "args": {}, "requires_confirmation": False, "reason": "Next track"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\bprevious track\b", text) or re.search(r"\bgo back\b", text):
        return {
            "plan": [{"tool": "media", "operation": "media_previous_track", "args": {}, "requires_confirmation": False, "reason": "Previous track"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\bwhat('?s| is) playing\b", text) or re.search(r"\bnow playing\b", text) or re.search(r"\bcurrently playing\b", text):
        return {
            "plan": [{"tool": "media", "operation": "query_playing", "args": {}, "requires_confirmation": False, "reason": "Query currently playing media"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    # Window management patterns
    maximize_match = re.match(r"(?:maximize|fullscreen) (.+)", text)
    if maximize_match:
        return {
            "plan": [{"tool": "window", "operation": "maximize_window", "args": {"title": maximize_match.group(1).strip()}, "requires_confirmation": False, "reason": "Maximize window"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    minimize_match = re.match(r"minimize (.+)", text)
    if minimize_match:
        return {
            "plan": [{"tool": "window", "operation": "minimize_window", "args": {"title": minimize_match.group(1).strip()}, "requires_confirmation": False, "reason": "Minimize window"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    restore_match = re.match(r"(?:restore|unminimize) (.+)", text)
    if restore_match:
        return {
            "plan": [{"tool": "window", "operation": "restore_window", "args": {"title": restore_match.group(1).strip()}, "requires_confirmation": False, "reason": "Restore window"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    close_match = re.match(r"(?:close|shut down) (.+)", text)
    if close_match and not is_terminal_command_allowed(close_match.group(1).strip())[0]:
        return {
            "plan": [{"tool": "window", "operation": "close_window", "args": {"title": close_match.group(1).strip()}, "requires_confirmation": True, "reason": "Close window"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    if re.search(r"\btile windows\b", text) or re.search(r"\barrange windows\b", text):
        return {
            "plan": [{"tool": "window", "operation": "tile_windows", "args": {}, "requires_confirmation": False, "reason": "Tile all visible windows"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    monitor_match = re.match(r"move (.+) to monitor (\d+)", text)
    if monitor_match:
        return {
            "plan": [{"tool": "window", "operation": "move_to_monitor", "args": {"title": monitor_match.group(1).strip(), "monitor": int(monitor_match.group(2)) - 1}, "requires_confirmation": False, "reason": "Move window to monitor"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    # Notification patterns
    notify_match = re.match(r"(?:remind me|notify me|tell me|alert me) (.+?) (?:in|after|within) (\d+) (?:minutes?|mins?|seconds?|hours?|hrs?)$", text)
    if notify_match:
        message = notify_match.group(1).strip()
        unit = notify_match.group(2).strip()
        return {
            "plan": [{"tool": "notification", "operation": "send_notification", "args": {"title": "Jarvis Reminder", "message": message}, "requires_confirmation": False, "reason": f"Send notification reminder: {message}"}],
            "needs_clarification": False,
            "clarification_question": "",
        }
    timer_match = re.match(r"(?:set a|timer|alarm) (.+)", text)
    if timer_match:
        return {
            "plan": [{"tool": "notification", "operation": "send_notification", "args": {"title": "Jarvis Timer", "message": timer_match.group(1).strip()}, "requires_confirmation": False, "reason": "Send timer notification"}],
            "needs_clarification": False,
            "clarification_question": "",
        }

    return None


def _split_app_target(value: str) -> tuple[str, str | None]:
    target = value.strip()
    for marker in (" in ", " on ", " inside "):
        if marker in target:
            left, right = target.rsplit(marker, 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                return left, right
    return target, None


def _normalize_key_token(token: str) -> str:
    alias_map = {
        "control": "ctrl",
        "ctrl": "ctrl",
        "command": "win",
        "windows": "win",
        "escape": "esc",
        "return": "enter",
        "spacebar": "space",
    }
    normalized = re.sub(r"[^a-z0-9+]", "", token.lower())
    return alias_map.get(normalized, normalized)


def _parse_shortcut_keys(text: str) -> list[str]:
    parts = re.split(r"(?:\s*\+\s*|\s+then\s+|\s+and\s+|\s+)", text.strip())
    keys = [_normalize_key_token(part) for part in parts if _normalize_key_token(part)]
    if len(keys) == 2 and keys[0] == "press":
        return [keys[1]]
    return [key for key in keys if key != "press"]


def _app_workflow_plan(text: str) -> dict[str, Any] | None:
    type_match = re.match(r"type\s+(.+)", text)
    if type_match:
        payload, app_name = _split_app_target(type_match.group(1))
        if payload:
            steps: list[dict[str, Any]] = []
            if app_name:
                steps.append(
                    {
                        "tool": "system",
                        "operation": "focus_app",
                        "args": {"name": app_name},
                        "requires_confirmation": False,
                        "reason": f"Focus {app_name} before typing.",
                    }
                )
            steps.append(
                {
                    "tool": "system",
                    "operation": "type_text",
                    "args": {"text": payload.strip("\"' ")},
                    "requires_confirmation": False,
                    "reason": "Type text into the active app.",
                }
            )
            return {"plan": steps, "needs_clarification": False, "clarification_question": ""}

    press_match = re.match(r"press\s+(.+)", text)
    if press_match:
        payload, app_name = _split_app_target(press_match.group(1))
        keys = _parse_shortcut_keys(payload)
        if keys:
            steps = []
            if app_name:
                steps.append(
                    {
                        "tool": "system",
                        "operation": "focus_app",
                        "args": {"name": app_name},
                        "requires_confirmation": False,
                        "reason": f"Focus {app_name} before sending keys.",
                    }
                )
            steps.append(
                {
                    "tool": "system",
                    "operation": "press_keys",
                    "args": {"keys": keys if len(keys) > 1 else keys[0]},
                    "requires_confirmation": False,
                    "reason": "Send shortcut keys to the active app.",
                }
            )
            return {"plan": steps, "needs_clarification": False, "clarification_question": ""}

    for prefix, operation in (("maximize ", "maximize_window"), ("minimize ", "minimize_window"), ("restore ", "restore_window")):
        if text.startswith(prefix):
            title = text[len(prefix):].strip()
            if title:
                return {
                    "plan": [
                        {
                            "tool": "window",
                            "operation": operation,
                            "args": {"title": title},
                            "requires_confirmation": False,
                            "reason": f"{operation.replace('_', ' ').title()} for {title}.",
                        }
                    ],
                    "needs_clarification": False,
                    "clarification_question": "",
                }

    focus_match = re.match(r"(?:focus|switch to)\s+(.+)", text)
    if focus_match:
        app_name = focus_match.group(1).strip()
        if app_name:
            return {
                "plan": [
                    {
                        "tool": "system",
                        "operation": "focus_app",
                        "args": {"name": app_name},
                        "requires_confirmation": False,
                        "reason": f"Focus the {app_name} window.",
                    }
                ],
                "needs_clarification": False,
                "clarification_question": "",
            }

    return None


def _mcp_heuristic_plan(text: str) -> dict[str, Any] | None:
    if text in {"list mcp servers", "show mcp servers", "what mcp servers are available"}:
        return {"plan": [{"tool": "mcp", "operation": "list_servers", "args": {}, "requires_confirmation": False, "reason": "List configured MCP servers."}], "needs_clarification": False, "clarification_question": ""}
    status_match = re.match(r"(?:show )?(?:mcp )?(?:server )?status (.+)", text)
    if status_match:
        return {"plan": [{"tool": "mcp", "operation": "server_status", "args": {"name": status_match.group(1).strip()}, "requires_confirmation": False, "reason": "Show MCP server status."}], "needs_clarification": False, "clarification_question": ""}
    tools_match = re.match(r"(?:list|show) (?:mcp )?tools (?:for|of) (.+)", text)
    if tools_match:
        return {"plan": [{"tool": "mcp", "operation": "list_server_tools", "args": {"name": tools_match.group(1).strip()}, "requires_confirmation": False, "reason": "List MCP tools for a server."}], "needs_clarification": False, "clarification_question": ""}
    resources_match = re.match(r"(?:list|show) (?:mcp )?resources (?:for|of) (.+)", text)
    if resources_match:
        return {"plan": [{"tool": "mcp", "operation": "list_server_resources", "args": {"name": resources_match.group(1).strip()}, "requires_confirmation": False, "reason": "List MCP resources for a server."}], "needs_clarification": False, "clarification_question": ""}
    reconnect_match = re.match(r"reconnect (?:mcp )?(?:server )?(.+)", text)
    if reconnect_match:
        return {"plan": [{"tool": "mcp", "operation": "reconnect_server", "args": {"name": reconnect_match.group(1).strip()}, "requires_confirmation": False, "reason": "Reconnect an MCP server."}], "needs_clarification": False, "clarification_question": ""}
    disable_match = re.match(r"disable (?:mcp )?(?:server )?(.+)", text)
    if disable_match:
        return {"plan": [{"tool": "mcp", "operation": "disable_server", "args": {"name": disable_match.group(1).strip()}, "requires_confirmation": True, "reason": "Disable an MCP server."}], "needs_clarification": False, "clarification_question": ""}
    return None


def _agent_heuristic_plan(text: str) -> dict[str, Any] | None:
    if text in {"list agents", "show agents", "show agent status", "what agents are running"}:
        return {"plan": [{"tool": "agent", "operation": "list_agents", "args": {}, "requires_confirmation": False, "reason": "List managed agents."}], "needs_clarification": False, "clarification_question": ""}
    start_match = re.match(r"(?:start|run|create) (?:an )?agent to (.+)", text)
    if start_match:
        return {"plan": [{"tool": "agent", "operation": "start_agent", "args": {"goal": start_match.group(1).strip()}, "requires_confirmation": False, "reason": "Start a managed agent."}], "needs_clarification": False, "clarification_question": ""}
    status_match = re.match(r"(?:show )?(?:status|state) (?:of )?agent (.+)", text)
    if status_match:
        return {"plan": [{"tool": "agent", "operation": "agent_status", "args": {"agent_id": status_match.group(1).strip()}, "requires_confirmation": False, "reason": "Show managed agent status."}], "needs_clarification": False, "clarification_question": ""}
    cancel_match = re.match(r"(?:cancel|stop) agent (.+)", text)
    if cancel_match:
        return {"plan": [{"tool": "agent", "operation": "cancel_agent", "args": {"agent_id": cancel_match.group(1).strip()}, "requires_confirmation": True, "reason": "Cancel a managed agent."}], "needs_clarification": False, "clarification_question": ""}
    result_match = re.match(r"(?:show|get) (?:result|output) (?:of )?agent (.+)", text)
    if result_match:
        return {"plan": [{"tool": "agent", "operation": "agent_result", "args": {"agent_id": result_match.group(1).strip()}, "requires_confirmation": False, "reason": "Fetch the latest agent result."}], "needs_clarification": False, "clarification_question": ""}
    if text in {"clear completed agents", "clear agent history"}:
        return {"plan": [{"tool": "agent", "operation": "clear_completed_agents", "args": {}, "requires_confirmation": True, "reason": "Clear completed agent history."}], "needs_clarification": False, "clarification_question": ""}
    return None


def _plan_with_ollama(user_input: str) -> dict[str, Any]:
    payload = {
        "model": settings.ollama_model,
        "prompt": (f"{SYSTEM_PROMPT}\n\nAvailable operations:\n{json.dumps(describe_capabilities(), indent=2)}\n\nUser request: {user_input}\nReturn only JSON."),
        "stream": False,
        "format": "json",
    }
    try:
        response = requests.post(f"{settings.ollama_base_url}/api/generate", json=payload, timeout=90)
        if response.status_code == 500:
            raise PlannerError(f"Ollama server error (500) at {settings.ollama_base_url}.")
        response.raise_for_status()
        body = response.json()
        if "response" not in body:
            raise PlannerError("Ollama response did not include a planner payload.")
        return _extract_json(body["response"])
    except requests.RequestException as exc:
        raise PlannerError(f"Could not reach Ollama at {settings.ollama_base_url}: {exc}") from exc


def _answer_with_ollama(user_input: str) -> str:
    payload = {
        "model": settings.ollama_model,
        "prompt": (f"You are Jarvis. Answer the user's question directly in 2 short sentences max. Do not propose opening a browser unless the user explicitly asked for it.\n\nUser question: {user_input}"),
        "stream": False,
    }
    try:
        response = requests.post(f"{settings.ollama_base_url}/api/generate", json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        answer = str(body.get("response", "")).strip()
        if not answer:
            raise PlannerError("Ollama returned an empty answer.")
        return answer
    except requests.RequestException as exc:
        raise PlannerError(f"Could not reach Ollama at {settings.ollama_base_url}: {exc}") from exc


def _answer_with_ollama_conversation(user_input: str, memory_lines: list[str] | None = None, recent_turns: list[dict[str, str]] | None = None) -> str:
    context_sections = [CONVERSATION_SYSTEM_PROMPT]
    memory_block = _memory_context_block(memory_lines)
    recent_block = _recent_conversation_block(recent_turns)
    if memory_block:
        context_sections.append(memory_block)
    if recent_block:
        context_sections.append(recent_block)
    prompt = "\n\n".join(context_sections)
    prompt = f"{prompt}\n\nUser: {user_input}\nAssistant:"
    payload = {"model": settings.ollama_model, "prompt": prompt, "stream": False}
    try:
        response = requests.post(f"{settings.ollama_base_url}/api/generate", json=payload, timeout=60)
        response.raise_for_status()
        body = response.json()
        answer = str(body.get("response", "")).strip()
        if not answer:
            raise PlannerError("Ollama returned an empty conversational answer.")
        return answer
    except requests.RequestException as exc:
        raise PlannerError(f"Could not reach Ollama at {settings.ollama_base_url}: {exc}") from exc


def _plan_with_groq(user_input: str) -> dict[str, Any]:
    if not Groq:
        raise PlannerError("Groq library is not installed.")
    if not settings.groq_api_key:
        raise PlannerError("Groq API key is not set. Please set GROQ_API_KEY in your environment.")
    client = Groq(api_key=settings.groq_api_key)
    try:
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Available operations:\n{json.dumps(describe_capabilities(), indent=2)}\n\nUser request: {user_input}"},
            ],
            response_format={"type": "json_object"},
        )
        return _extract_json(completion.choices[0].message.content)
    except Exception as exc:
        raise PlannerError(f"Groq API error: {exc}") from exc


def _answer_with_groq(user_input: str) -> str:
    if not Groq:
        raise PlannerError("Groq library is not installed.")
    if not settings.groq_api_key:
        raise PlannerError("Groq API key is not set.")
    client = Groq(api_key=settings.groq_api_key)
    try:
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": "You are Jarvis. Answer the user's question directly in 2 short sentences max. Do not suggest opening a browser unless they asked for that."},
                {"role": "user", "content": user_input},
            ],
        )
        answer = str(completion.choices[0].message.content or "").strip()
        if not answer:
            raise PlannerError("Groq returned an empty answer.")
        return answer
    except Exception as exc:
        raise PlannerError(f"Groq API error: {exc}") from exc


def _answer_with_groq_conversation(user_input: str, memory_lines: list[str] | None = None, recent_turns: list[dict[str, str]] | None = None) -> str:
    if not Groq:
        raise PlannerError("Groq library is not installed.")
    if not settings.groq_api_key:
        raise PlannerError("Groq API key is not set.")
    client = Groq(api_key=settings.groq_api_key)
    messages: list[dict[str, str]] = [{"role": "system", "content": CONVERSATION_SYSTEM_PROMPT}]
    memory_block = _memory_context_block(memory_lines)
    if memory_block:
        messages.append({"role": "system", "content": memory_block})
    if recent_turns:
        for turn in recent_turns[-6:]:
            role = str(turn.get("role", "assistant")).strip().lower()
            if role not in {"user", "assistant"}:
                role = "assistant"
            text = str(turn.get("text", "")).strip()
            if text:
                messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_input})
    try:
        completion = client.chat.completions.create(model=settings.groq_model, messages=messages)
        answer = str(completion.choices[0].message.content or "").strip()
        if not answer:
            raise PlannerError("Groq returned an empty conversational answer.")
        return answer
    except Exception as exc:
        raise PlannerError(f"Groq API error: {exc}") from exc


def answer_question(user_input: str) -> str:
    try:
        answer, _provider = _run_with_provider_fallback(
            "answer_question",
            {
                "ollama": lambda: _answer_with_ollama(user_input),
                "groq": lambda: _answer_with_groq(user_input),
            },
        )
        return answer
    except PlannerError as exc:
        raise


def answer_conversation(user_input: str, memory_lines: list[str] | None = None, recent_turns: list[dict[str, str]] | None = None) -> str:
    try:
        answer, _provider = _run_with_provider_fallback(
            "answer_conversation",
            {
                "ollama": lambda: _answer_with_ollama_conversation(user_input, memory_lines=memory_lines, recent_turns=recent_turns),
                "groq": lambda: _answer_with_groq_conversation(user_input, memory_lines=memory_lines, recent_turns=recent_turns),
            },
        )
        return answer
    except PlannerError as exc:
        text = user_input.strip().lower()
        if _looks_social(text):
            return answer_social(text)
        return answer_question(user_input)


def plan_command(user_input: str) -> dict[str, Any]:
    h_plan = _heuristic_plan(user_input)
    if h_plan:
        _record_planner_status(True, "heuristic", "plan_command", "Matched heuristic planner.")
        return h_plan
    try:
        plan, _provider = _run_with_provider_fallback(
            "plan_command",
            {
                "ollama": lambda: _plan_with_ollama(user_input),
                "groq": lambda: _plan_with_groq(user_input),
            },
        )
        return plan
    except Exception as exc:
        return {
            "plan": [],
            "needs_clarification": True,
            "clarification_question": "I could not build a safe action plan. Please restate the exact app, URL, file path, or allowed command.",
        }
