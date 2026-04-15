from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.config import settings
from system.tools import ToolResult


LOGGER = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("Failed to read JSON from %s", path)
        return default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass(slots=True)
class AgentState:
    id: str
    name: str
    goal: str
    status: str
    created_at: str
    updated_at: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    result_summary: str = ""
    error: str = ""
    constraints: str = ""


class AgentManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._load()

    def _load(self) -> None:
        payload = _read_json(settings.agent_state_path, {"agents": []})
        agents = {}
        for item in payload.get("agents", []):
            if not isinstance(item, dict):
                continue
            state = AgentState(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                goal=str(item.get("goal", "")),
                status=str(item.get("status", "unknown")),
                created_at=str(item.get("created_at", _utcnow_iso())),
                updated_at=str(item.get("updated_at", _utcnow_iso())),
                steps=list(item.get("steps", [])),
                result_summary=str(item.get("result_summary", "")),
                error=str(item.get("error", "")),
                constraints=str(item.get("constraints", "")),
            )
            if state.id:
                agents[state.id] = state
        self._agents = agents

    def _save(self) -> None:
        _write_json(settings.agent_state_path, {"agents": [asdict(agent) for agent in self._agents.values()]})

    def list_agents(self, status: str = "") -> ToolResult:
        with self._lock:
            agents = [asdict(agent) for agent in self._agents.values() if not status or agent.status == status]
        return ToolResult(True, f"Found {len(agents)} agent(s).", {"source_kind": "agent", "agents": agents})

    def resolve_agent_id(self, reference: str) -> str | None:
        normalized = reference.strip().lower()
        with self._lock:
            exact = [agent.id for agent in self._agents.values() if agent.id.lower() == normalized or agent.name.lower() == normalized]
            if exact:
                return exact[0]
            partial = [agent.id for agent in self._agents.values() if normalized in agent.id.lower() or normalized in agent.name.lower()]
        if len(partial) == 1:
            return partial[0]
        return None

    def start_agent(self, goal: str, name: str = "", constraints: str = "") -> ToolResult:
        goal = goal.strip()
        if not goal:
            return ToolResult(False, "Agent goal is required.")
        now = _utcnow_iso()
        agent_id = str(uuid.uuid4())[:8]
        state = AgentState(
            id=agent_id,
            name=name.strip() or f"agent-{agent_id}",
            goal=goal,
            status="running",
            created_at=now,
            updated_at=now,
            constraints=constraints.strip(),
        )
        cancel_event = threading.Event()
        with self._lock:
            self._agents[agent_id] = state
            self._cancel_events[agent_id] = cancel_event
            self._save()
        thread = threading.Thread(target=self._run_agent, args=(agent_id,), daemon=True, name=f"jarvis-agent-{agent_id}")
        thread.start()
        return ToolResult(
            True,
            f"Started agent {state.name} ({agent_id}).",
            {"source_kind": "agent", "agent": asdict(state)},
        )

    def agent_status(self, agent_id: str) -> ToolResult:
        resolved = self.resolve_agent_id(agent_id)
        if not resolved:
            return ToolResult(False, f"I need the exact agent id or name for '{agent_id}'.", {"reason": "clarification_needed"})
        with self._lock:
            state = self._agents[resolved]
            payload = asdict(state)
        return ToolResult(True, f"Agent {state.name} is {state.status}.", {"source_kind": "agent", "agent": payload})

    def cancel_agent(self, agent_id: str) -> ToolResult:
        resolved = self.resolve_agent_id(agent_id)
        if not resolved:
            return ToolResult(False, f"I need the exact agent id or name for '{agent_id}'.", {"reason": "clarification_needed"})
        with self._lock:
            state = self._agents[resolved]
            cancel_event = self._cancel_events.setdefault(resolved, threading.Event())
            if state.status in {"completed", "failed", "cancelled"}:
                return ToolResult(True, f"Agent {state.name} is already {state.status}.", {"source_kind": "agent", "agent": asdict(state)})
            cancel_event.set()
            state.status = "cancelled"
            state.updated_at = _utcnow_iso()
            self._save()
            payload = asdict(state)
        return ToolResult(True, f"Cancelled agent {state.name}.", {"source_kind": "agent", "agent": payload})

    def agent_result(self, agent_id: str) -> ToolResult:
        resolved = self.resolve_agent_id(agent_id)
        if not resolved:
            return ToolResult(False, f"I need the exact agent id or name for '{agent_id}'.", {"reason": "clarification_needed"})
        with self._lock:
            state = self._agents[resolved]
            payload = asdict(state)
        return ToolResult(True, state.result_summary or f"Agent {state.name} has no result yet.", {"source_kind": "agent", "agent": payload})

    def clear_completed_agents(self) -> ToolResult:
        with self._lock:
            kept = {agent_id: state for agent_id, state in self._agents.items() if state.status not in {"completed", "failed", "cancelled"}}
            removed = len(self._agents) - len(kept)
            self._cancel_events = {agent_id: event for agent_id, event in self._cancel_events.items() if agent_id in kept}
            self._agents = kept
            self._save()
        return ToolResult(True, f"Cleared {removed} completed agent record(s).", {"source_kind": "agent", "removed": removed})

    def _update_state(self, agent_id: str, **changes: Any) -> AgentState:
        with self._lock:
            state = self._agents[agent_id]
            for key, value in changes.items():
                setattr(state, key, value)
            state.updated_at = _utcnow_iso()
            self._save()
            return state

    def _run_agent(self, agent_id: str) -> None:
        with self._lock:
            state = self._agents[agent_id]
            cancel_event = self._cancel_events[agent_id]
        try:
            if cancel_event.is_set():
                return
            from core.executor import execute_plan
            from core.planner import plan_command

            plan = plan_command(state.goal)
            steps = list(plan.get("plan", []))[: settings.agent_step_limit]
            if any(str(step.get("tool")) == "agent" for step in steps if isinstance(step, dict)):
                raise RuntimeError("Agents cannot spawn or manage other agents in v1.")
            self._update_state(agent_id, steps=steps)
            if cancel_event.is_set():
                self._update_state(agent_id, status="cancelled", result_summary="Cancelled before execution.")
                return
            result = execute_plan(plan)
            summary = self._result_summary(result)
            if cancel_event.is_set():
                self._update_state(agent_id, status="cancelled", result_summary="Cancelled during execution.")
                return
            if result.get("status") == "completed":
                self._update_state(agent_id, status="completed", result_summary=summary)
            elif result.get("status") == "clarification_needed":
                self._update_state(agent_id, status="failed", result_summary=summary, error=summary)
            else:
                self._update_state(agent_id, status="failed", result_summary=summary, error=summary)
        except Exception as exc:
            LOGGER.exception("Agent %s failed.", agent_id)
            self._update_state(agent_id, status="failed", result_summary=str(exc), error=str(exc))
        finally:
            time.sleep(settings.agent_poll_interval_seconds)

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            agents = [asdict(agent) for agent in self._agents.values()]
        return {"agents": agents}

    def _result_summary(self, result: dict[str, Any]) -> str:
        status = str(result.get("status", "")).strip().lower()
        if status == "completed":
            messages = [str(item.get("message", "")).strip() for item in result.get("results") or [] if str(item.get("message", "")).strip()]
            if messages:
                return ". ".join(messages)
        return str(result.get("message", "")).strip()


_AGENT_MANAGER = AgentManager()


def get_agent_manager() -> AgentManager:
    return _AGENT_MANAGER
