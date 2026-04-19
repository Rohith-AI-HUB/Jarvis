from __future__ import annotations

import json
import inspect
import logging
import re
from typing import Any

from core.capabilities import resolve_handler
from system.config import settings
from system.tools import CONFIRM, ToolResult


logging.basicConfig(
    filename=settings.logs_path,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


class ExecutionError(RuntimeError):
    pass


class ConfirmationInterruption(RuntimeError):
    def __init__(self, command: str) -> None:
        super().__init__(command)
        self.command = command


def explain_step(step: dict[str, Any]) -> str:
    return f"{step['tool']}.{step['operation']} with args {json.dumps(step.get('args', {}), ensure_ascii=True)}"


def _parse_confirmation(value: str) -> bool | None:
    normalized = re.sub(r"[^a-z0-9\s]", "", value.lower()).strip()
    if not normalized:
        return None
    yes_tokens = {"y", "yes", "yeah", "yep", "ok", "okay", "confirm", "continue", "sure", "do it", "go ahead"}
    no_tokens = {"n", "no", "nope", "cancel", "stop", "dont", "do not", "abort"}
    if normalized in yes_tokens:
        return True
    if normalized in no_tokens:
        return False
    if "yes" in normalized or "continue" in normalized:
        return True
    if "no" in normalized or "cancel" in normalized or "stop" in normalized:
        return False
    return None


def _simplified_confirmation_prompt(step: dict[str, Any]) -> str:
    operation = step.get("operation", "this action").replace("_", " ")
    return f"Should I continue with {operation}? Please say yes or no."


def confirm_step(
    step: dict[str, Any],
    speaker: Any | None = None,
    session_origin: str = "text",
    voice_confirmation_handler: Any | None = None,
) -> bool:
    explanation = step.get("reason") or explain_step(step)
    prompt = f"Jarvis wants to {explanation}. Continue?"

    if session_origin == "voice" and voice_confirmation_handler:
        if speaker:
            speaker.say(f"I need confirmation. {explanation}")
        first_answer = str(voice_confirmation_handler(prompt)).strip()
        first_decision = _parse_confirmation(first_answer)
        if first_decision is not None:
            return first_decision
        if first_answer:
            raise ConfirmationInterruption(first_answer)
        simplified_prompt = _simplified_confirmation_prompt(step)
        if speaker:
            speaker.say(f"I need a clear answer. {simplified_prompt}")
        second_answer = str(voice_confirmation_handler(simplified_prompt)).strip()
        second_decision = _parse_confirmation(second_answer)
        if second_decision is None and second_answer:
            raise ConfirmationInterruption(second_answer)
        return second_decision is True

    prompt_with_suffix = f"{prompt} [y/N]: "
    answer = input(prompt_with_suffix).strip().lower()
    if answer in {"why", "explain"}:
        detail = f"This action uses {step['tool']}:{step['operation']} and may change your system or files."
        print(detail)
        if speaker:
            speaker.say(detail)
        answer = input(prompt_with_suffix).strip().lower()
    decision = _parse_confirmation(answer)
    return decision is True


def validate_plan(plan_data: dict[str, Any]) -> list[dict[str, Any]]:
    steps = plan_data.get("plan", [])
    if not isinstance(steps, list):
        raise ExecutionError("Plan must contain a list of steps.")
    for step in steps:
        if not isinstance(step, dict):
            raise ExecutionError("Each plan step must be an object.")
        handler, risk = resolve_handler(step.get("tool", ""), step.get("operation", ""))
        if not handler:
            raise ExecutionError(f"Unknown tool operation: {step}")
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise ExecutionError(f"Plan step args must be an object: {step}")
        try:
            inspect.signature(handler).bind(**args)
        except TypeError as exc:
            raise ExecutionError(f"Invalid arguments for {step.get('tool')}.{step.get('operation')}: {exc}") from exc
        step.setdefault("requires_confirmation", risk == CONFIRM)
        step.setdefault("reason", "")
    return steps


def _invalid_plan_result(plan_data: Any, error: str) -> dict[str, Any]:
    try:
        raw_plan = json.dumps(plan_data, ensure_ascii=True)
    except Exception:
        raw_plan = repr(plan_data)
    logging.error("Invalid planner output: %s | raw=%s", error, raw_plan)
    return {
        "status": "invalid_plan",
        "message": "That plan was invalid, so I stopped safely.",
        "results": [],
        "error": error,
        "plan": plan_data,
    }


def execute_plan(
    plan_data: dict[str, Any],
    speaker: Any | None = None,
    session_origin: str = "text",
    voice_confirmation_handler: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(plan_data, dict):
        return _invalid_plan_result(plan_data, "Plan payload must be an object.")

    if plan_data.get("needs_clarification"):
        return {
            "status": "clarification_needed",
            "message": plan_data.get("clarification_question") or "I need more detail before I can act.",
            "results": [],
        }

    try:
        steps = validate_plan(plan_data)
    except ExecutionError as exc:
        return _invalid_plan_result(plan_data, str(exc))

    results: list[dict[str, Any]] = []
    for step in steps:
        handler, risk = resolve_handler(step["tool"], step["operation"])
        requires_confirmation = bool(step.get("requires_confirmation") or risk == CONFIRM)
        if session_origin == "voice" and risk == CONFIRM:
            requires_confirmation = True
        if requires_confirmation:
            try:
                confirmed = confirm_step(
                    step,
                    speaker=speaker,
                    session_origin=session_origin,
                    voice_confirmation_handler=voice_confirmation_handler,
                )
            except ConfirmationInterruption as exc:
                return {
                    "status": "interrupted",
                    "message": "Received a new voice command during confirmation.",
                    "results": results,
                    "interrupted_command": exc.command,
                }
            if not confirmed:
                return {
                    "status": "cancelled",
                    "message": f"Cancelled safely at step {step['operation']}",
                    "results": results,
                }
        logging.info("Executing step: %s", explain_step(step))
        result: ToolResult = handler(**step.get("args", {}))
        results.append(
            {
                "step": step,
                "ok": result.ok,
                "message": result.message,
                "data": result.data or {},
            }
        )
        if not result.ok:
            logging.error("Step failed: %s", result.message)
            data = result.data or {}
            if data.get("reason") == "clarification_needed":
                return {
                    "status": "clarification_needed",
                    "message": result.message,
                    "results": results,
                }
            return {
                "status": "failed",
                "message": result.message,
                "results": results,
            }
    return {
        "status": "completed",
        "message": "Plan executed successfully.",
        "results": results,
    }
