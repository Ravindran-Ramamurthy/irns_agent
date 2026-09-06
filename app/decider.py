import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app import domain, reasoning, store
from app.server_client import ServerClient

log = logging.getLogger(__name__)

Decision = Dict[str, Any]


class Decider(Protocol):
    """Same contract regardless of implementation - graph.py calls whichever
    one store.get_decider_mode(tenant_key) picks and processes the returned
    Decision identically either way (build_action_event_payload,
    resolve_new_status_id etc. in reasoning.py don't know or care which
    produced it)."""

    async def decide_for_risk_event(
            self, entry: domain.IngestResult, risk_event: Dict[str, Any]
    ) -> Tuple[Decision, Optional[Dict[str, int]]]: ...

    async def decide_for_incoming_event(
            self, entry: domain.IngestResult, risk_event: Dict[str, Any],
            action_events: List[Dict[str, Any]], incoming_events: List[Dict[str, Any]],
            incoming_event: Dict[str, Any]
    ) -> Tuple[Decision, Optional[Dict[str, int]]]: ...

    async def decide_for_action_event(
            self, entry: domain.IngestResult, action_event: Dict[str, Any], risk_event: Dict[str, Any],
            action_events: List[Dict[str, Any]], incoming_events: List[Dict[str, Any]]
    ) -> Tuple[Decision, Optional[Dict[str, int]]]: ...


class LlmDecider:
    """Thin wrapper around reasoning.decide() - unchanged behavior, just
    reshaped to the 3-method Decider contract instead of one decide(source=...)
    function."""

    def __init__(self, client: ServerClient):
        self._client = client

    async def decide_for_risk_event(self, entry, risk_event):
        return await reasoning.decide(self._client, "riskevent", entry, risk_event)

    async def decide_for_incoming_event(self, entry, risk_event, action_events, incoming_events, incoming_event):
        return await reasoning.decide(
            self._client, "incomingevent", entry, risk_event,
            incoming_event=incoming_event, action_events=action_events, incoming_events=incoming_events)

    async def decide_for_action_event(self, entry, action_event, risk_event, action_events, incoming_events):
        return await reasoning.decide(
            self._client, "actionevent", entry, risk_event,
            action_events=action_events, incoming_events=incoming_events)


def _unmodeled(reason: str) -> Decision:
    """No structured JSON to act on (missing, unparseable, or references a
    step/branch that doesn't exist) - route to the agent queue rather than
    guess, and stop scheduling further reviews (reviewAt=None). Mirrors the
    LLM path's own "no configured workflow" fallback message, but as a
    concrete deterministic decision instead of prose for a model to read."""
    log.warning("LocalDecider: %s - escalating to IRNS Dispute queue", reason)
    return {
        "actionType": "MOVE_QUEUE",
        "actionCode": None,
        "channel": None,
        "metadata": {"queue": "IRNS Dispute", "reason": reason},
        "comments": reason,
        "newStatusCode": None,
        "reviewAt": None,
    }


def _parse_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _first_step(workflow: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return next((s for s in workflow.get("steps", []) if s.get("on") == "ALERT_RECEIVED"), None)


def _decision_from_step(step: Dict[str, Any]) -> Decision:
    actions = step.get("actions") or []
    primary = actions[0] if actions else {}
    extra_actions = actions[1:]

    metadata: Dict[str, Any] = {}
    if step.get("id"):
        metadata["stepId"] = step["id"]
    if extra_actions:
        # The action_event/Decision shape is one actionType/actionCode/channel
        # per row - a step with several simultaneous actions (e.g. block IB
        # + notify old and new mobile/email) can't be split across several
        # rows here, so only the first becomes the row's own action; the
        # rest are preserved, not dropped, as data for whoever executes it.
        metadata["additionalActions"] = extra_actions

    terminal = bool(step.get("terminal"))
    wait_seconds = step.get("waitSeconds")
    review_at = None
    if not terminal and isinstance(wait_seconds, (int, float)):
        review_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)).strftime("%Y-%m-%dT%H:%M:%S")

    comments = step.get("note") or step.get("description") or f"Step {step.get('id') or '(response handling)'}"

    return {
        "actionType": primary.get("actionType"),
        "actionCode": primary.get("actionCode"),
        "channel": primary.get("channel"),
        "metadata": metadata,
        "comments": comments,
        "newStatusCode": step.get("newStatusCode"),
        "reviewAt": review_at,
    }


class LocalDecider:
    """Parses WORKFLOW_JSON/RESPONSE_CODE_JSON directly and computes a
    Decision without any LLM call - active when this tenant's decider mode
    (see app/store.py's get_decider_mode()) is "local". Fetches the one
    alert/response code row + its JSON prompt an event's own FK id points at
    live, via app/domain.py's load_alert_code()/load_response_code() - not
    from a bulk preload."""

    def __init__(self, client: ServerClient):
        self._client = client

    async def _workflow_json(self, alert_code_id: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        row, instructions = await domain.load_alert_code(self._client, alert_code_id, "WORKFLOW_JSON")
        return row, _parse_json(instructions)

    async def decide_for_risk_event(self, entry: domain.IngestResult, risk_event: Dict[str, Any]):
        alert_code_id = risk_event.get("alertCodeId")
        row, workflow = await self._workflow_json(alert_code_id)
        if not workflow:
            return _unmodeled(f"no WORKFLOW_JSON configured for alert code {row['code'] if row else alert_code_id!r}"), None

        first_step = _first_step(workflow)
        if not first_step:
            return _unmodeled("WORKFLOW_JSON has no ALERT_RECEIVED step"), None

        return _decision_from_step(first_step), None

    async def decide_for_incoming_event(self, entry: domain.IngestResult, risk_event: Dict[str, Any],
                                         action_events: List[Dict[str, Any]], incoming_events: List[Dict[str, Any]],
                                         incoming_event: Dict[str, Any]):
        response_code_id = incoming_event.get("responseCodeId")
        row, instructions = await domain.load_response_code(self._client, response_code_id, "RESPONSE_CODE_JSON")
        rc = _parse_json(instructions)
        if not rc:
            return _unmodeled(f"no RESPONSE_CODE_JSON configured for response code "
                               f"{row['code'] if row else response_code_id!r}"), None

        if "ifIbCurrentlyBlocked" in rc or "ifIbNotBlocked" in rc:
            branch = rc["ifIbCurrentlyBlocked"] if _is_ib_currently_blocked(entry, action_events) else rc["ifIbNotBlocked"]
        else:
            branch = rc

        return _decision_from_step(branch), None

    async def decide_for_action_event(self, entry: domain.IngestResult, action_event: Dict[str, Any],
                                       risk_event: Dict[str, Any], action_events: List[Dict[str, Any]],
                                       incoming_events: List[Dict[str, Any]]):
        alert_code_id = risk_event.get("alertCodeId")
        row, workflow = await self._workflow_json(alert_code_id)
        if not workflow:
            return _unmodeled(f"no WORKFLOW_JSON configured for alert code {row['code'] if row else alert_code_id!r}"), None

        current_id = _current_step_id(action_events)
        if current_id is None:
            first_step = _first_step(workflow)
            if not first_step:
                return _unmodeled("WORKFLOW_JSON has no ALERT_RECEIVED step"), None
            return _decision_from_step(first_step), None

        steps = {s["id"]: s for s in workflow.get("steps", []) if s.get("id")}
        current = steps.get(current_id)
        if not current:
            return _unmodeled(f"WORKFLOW_JSON has no step {current_id!r} referenced by the latest action event"), None

        next_id = current.get("next")
        if not next_id:
            return _unmodeled(f"step {current_id!r} has no next step defined in WORKFLOW_JSON"), None

        next_step = steps.get(next_id)
        if not next_step:
            return _unmodeled(f"WORKFLOW_JSON's next step {next_id!r} does not exist"), None

        return _decision_from_step(next_step), None


def _with_parsed_metadata(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str):
        return _parse_json(meta)
    return None


def _current_step_id(action_events: List[Dict[str, Any]]) -> Optional[str]:
    for ae in sorted(action_events, key=lambda a: a.get("actionAt") or "", reverse=True):
        meta = _with_parsed_metadata(ae)
        if meta and meta.get("stepId"):
            return meta["stepId"]
    return None


def _action_type_code_by_id(entry: domain.IngestResult, action_type_id: Optional[str]) -> Optional[str]:
    if not action_type_id:
        return None
    for code, row in (entry.masters.get("ACTION_TYPE") or {}).items():
        if row.get("id") == action_type_id:
            return code
    return None


def _is_ib_currently_blocked(entry: domain.IngestResult, action_events: List[Dict[str, Any]]) -> bool:
    for ae in sorted(action_events, key=lambda a: a.get("actionAt") or "", reverse=True):
        code = _action_type_code_by_id(entry, ae.get("actionTypeId"))
        if code == "IB_BLOCK":
            return True
        if code == "IB_UNBLOCK":
            return False
    return False


def build_decider(client: ServerClient, tenant_key: str) -> Decider:
    if store.get_decider_mode(tenant_key) == "local":
        return LocalDecider(client)
    return LlmDecider(client)
