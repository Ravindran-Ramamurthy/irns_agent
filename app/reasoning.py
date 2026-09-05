import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.prompts import PromptTemplate

from app import domain, prompts, settings
from app.server_client import ServerError

log = logging.getLogger(__name__)

# One PromptTemplate per lane, purely for formatting the query text - the
# model call itself still goes through smartgateway's /agent/llm-call (see
# ServerClient.call_llm), never a direct provider call. See app/graph.py's
# _decide() for which lane uses which.
_RISK_EVENT_QUERY_TEMPLATE = PromptTemplate.from_template("RISK EVENT:\n{risk_event_json}")
_INCOMING_EVENT_QUERY_TEMPLATE = PromptTemplate.from_template(
    "RISK EVENT:\n{risk_event_json}\n\nINCOMING EVENT:\n{incoming_event_json}")
_ACTION_EVENT_QUERY_TEMPLATE = PromptTemplate.from_template(
    "RISK EVENT:\n{risk_event_json}\n\nACTION HISTORY:\n{action_events_json}\n\n"
    "INCOMING EVENTS:\n{incoming_events_json}")

# Lane name -> (system prompt key, context prompt key). No local fallback
# text - Java's PromptScopeRegistry is the sole source of default prompt
# text; if a tenant hasn't got one resolvable yet, decide() turns that into
# a ServerError so the run is skipped cleanly instead of calling the LLM
# with missing instructions.
_LANE_PROMPT_KEYS = {
    "riskevent": ("risk_event_system", "risk_event_context"),
    "incomingevent": ("incoming_event_system", "incoming_event_context"),
    "actionevent": ("action_event_system", "action_event_context"),
}


def _master_options_text(entry: domain.IngestResult, master_type: str) -> str:
    """code (name) for every active row of one master table - sent as part
    of the LLM's context so a row ops adds to irns_action_type_master/
    irns_status_master actually becomes choosable immediately, with no
    prompt or code change needed."""
    rows = entry.masters.get(master_type) or {}
    if not rows:
        return "(none configured)"
    return ", ".join(
        f"{code} ({row['name']})" if row.get("name") else code
        for code, row in sorted(rows.items()))


def _reference_data_text(entry: domain.IngestResult) -> str:
    # The model has no other way to know what time it is "now" - every
    # timestamp in the risk event/action/incoming-event JSON is history,
    # not a clock. Without this, "review in 15 minutes" has nothing
    # reliable to be computed relative to, and reviewAt can end up
    # anchored to a stale timestamp from the data instead of the actual
    # moment of this call (observed: reviewAt earlier than the action's
    # own actionAt).
    return (
        f"CURRENT DATE/TIME (UTC): {datetime.now(timezone.utc).isoformat()}\n"
        f"AVAILABLE ACTION TYPES: {_master_options_text(entry, 'ACTION_TYPE')}\n"
        f"AVAILABLE STATUS CODES: {_master_options_text(entry, 'STATUS')}"
    )


async def _workflow_context(client, alert_code_id: Optional[str]) -> Tuple[str, str]:
    """Live per-event fetch (app/domain.py's load_alert_code(), not a bulk
    preload) of the one alert code a risk/action event's own alertCodeId
    points at, plus its prose workflow prompt. Returns (workflow_name,
    workflow_instructions_text)."""
    row, instructions = await domain.load_alert_code(client, alert_code_id, "WORKFLOW_INSTRUCTIONS")
    name = (row.get("name") or row.get("code")) if row else "unknown alert type"
    if instructions:
        return name, instructions
    return name, ("(no workflow instructions are configured for this alert code yet - do not guess an "
                  "action; set actionType to null, newStatusCode to a manual/escalation status if one is "
                  "available in AVAILABLE STATUS CODES, and explain in comments that this alert type has "
                  "no configured workflow)")


async def _response_code_instructions_text(client, incoming_event: Optional[Dict[str, Any]]) -> str:
    if not incoming_event:
        return "(not applicable - no incoming event for this decision)"
    _row, instructions = await domain.load_response_code(
        client, incoming_event.get("responseCodeId"), "RESPONSE_CODE_INSTRUCTIONS")
    if instructions:
        return instructions
    return ("(no instructions are configured for this response code yet - do not guess an action; "
            "set actionType to null, newStatusCode to a manual/escalation status if one is available "
            "in AVAILABLE STATUS CODES, and explain in comments that this response code has no "
            "configured handling)")


def _with_parsed_metadata(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Java serializes the jsonb metadata column as an escaped JSON string
    (same convention as ivr_item.action) - unescape it before embedding in
    the LLM's context so the model sees plain nested JSON instead of a
    string full of backslashes. Purely cosmetic: never touches what gets
    sent back to the server."""
    if not isinstance(obj, dict) or not obj.get("metadata"):
        return obj
    parsed = dict(obj)
    try:
        parsed["metadata"] = json.loads(obj["metadata"])
    except (json.JSONDecodeError, TypeError):
        pass
    return parsed


def _query_text(source: str, risk_event: Dict[str, Any], incoming_event: Optional[Dict[str, Any]],
                 action_events: List[Dict[str, Any]], incoming_events: List[Dict[str, Any]]) -> str:
    risk_event = _with_parsed_metadata(risk_event)
    action_events = [_with_parsed_metadata(a) for a in action_events]
    incoming_events = [_with_parsed_metadata(i) for i in incoming_events]
    if source == "riskevent":
        return _RISK_EVENT_QUERY_TEMPLATE.format(risk_event_json=json.dumps(risk_event, default=str))
    if source == "incomingevent":
        return _INCOMING_EVENT_QUERY_TEMPLATE.format(
            risk_event_json=json.dumps(risk_event, default=str),
            incoming_event_json=json.dumps(_with_parsed_metadata(incoming_event), default=str))
    return _ACTION_EVENT_QUERY_TEMPLATE.format(
        risk_event_json=json.dumps(risk_event, default=str),
        action_events_json=json.dumps(action_events, default=str),
        incoming_events_json=json.dumps(incoming_events, default=str))


def _normalize_review_at(value: Any) -> Optional[str]:
    """Java's reviewAt columns are plain LocalDateTime - no offset/zone
    accepted, and a mismatched shape 500s the create call rather than
    failing gracefully. The LLM is shown CURRENT DATE/TIME with a UTC
    offset (see _reference_data_text) and tends to imitate that shape back in
    reviewAt, so normalize whatever comes back - 'Z', '+HH:MM', or already
    offset-less - into naive 'yyyy-MM-ddTHH:mm:ss' UTC. Drops the value
    entirely if it can't be parsed at all: better to skip the review time
    than fail the whole decision."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        log.warning("Could not parse reviewAt %r from the LLM - dropping it", value)
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_decision(reply: str) -> Dict[str, Any]:
    """The LLM is asked for a bare JSON object but may still wrap it in a
    markdown code fence - strip that before parsing, same defensive step
    every other agent's own JSON-reply handling takes."""
    text = reply.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Could not parse a decision JSON object from the LLM reply: %r", reply[:300])
        return {}
    if not isinstance(decision, dict):
        return {}
    if decision.get("reviewAt"):
        decision["reviewAt"] = _normalize_review_at(decision["reviewAt"])
    return decision


async def decide(client, source: str, entry: domain.IngestResult, risk_event: Dict[str, Any],
                  incoming_event: Optional[Dict[str, Any]] = None,
                  action_events: Optional[List[Dict[str, Any]]] = None,
                  incoming_events: Optional[List[Dict[str, Any]]] = None
                  ) -> "tuple[Dict[str, Any], Optional[Dict[str, Any]]]":
    """Shared by all 3 lanes - only the prompt keys and the query text differ.
    Returns (decision, usage); decision is {} if the model's reply couldn't
    be parsed into one."""
    system_key, context_key = _LANE_PROMPT_KEYS[source]

    overrides = await client.get_prompts("Shared")
    try:
        system = prompts.get(system_key, overrides)
        alert_code_id = risk_event.get("alertCodeId")
        workflow_name, workflow_instructions = await _workflow_context(client, alert_code_id)
        response_code_instructions = await _response_code_instructions_text(client, incoming_event)
        context = prompts.render(
            context_key, overrides,
            workflow_name=workflow_name,
            workflow_instructions=workflow_instructions,
            response_code_instructions=response_code_instructions,
            reference_data=_reference_data_text(entry))
    except KeyError as exc:
        raise ServerError(str(exc)) from exc
    query = _query_text(source, risk_event, incoming_event, action_events or [], incoming_events or [])

    reply, _statuses, usage = await client.call_llm(settings.AGENT_ID, system, context, query)
    return _parse_decision(reply), usage


def build_action_event_payload(entry: domain.IngestResult, risk_event_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Always builds a payload now - even a "nothing to do yet, recheck at
    reviewAt" decision is its own action_event row (actionTypeId left null),
    never a mutation of a prior row. actionTypeId is only left null if the
    decision itself has no actionType, or if that code doesn't map to a
    known irns_action_type_master row."""
    action_type_id = None
    action_type_code = decision.get("actionType")
    if action_type_code:
        action_type_id = domain.master_id_for_code(entry, "ACTION_TYPE", action_type_code)
        if not action_type_id:
            log.warning("Decision actionType %r did not map to a known action type - "
                        "recording this action with no action type", action_type_code)

    return {
        "riskEventId": risk_event_id,
        "actionTypeId": action_type_id,
        "actionCode": decision.get("actionCode"),
        "channel": decision.get("channel"),
        # Raw JSON text, matching the jsonb column - how to actually carry
        # out this action (e.g. an email/SMS template + variables). The
        # server binds this straight through as ::jsonb, same convention as
        # ivr_item.action.
        "metadata": json.dumps(decision.get("metadata") or {}),
        # Always PENDING - this agent decides and records, it doesn't
        # execute. Most rows stay PENDING indefinitely, and that's fine.
        "actionStatusId": domain.master_id_for_code(entry, "ACTION_STATUS", "PENDING"),
        "comments": decision.get("comments"),
        "reviewAt": decision.get("reviewAt"),
    }


def resolve_new_status_id(entry: domain.IngestResult, decision: Dict[str, Any]) -> Optional[str]:
    code = decision.get("newStatusCode")
    return domain.master_id_for_code(entry, "STATUS", code) if code else None


def default_closed_status_id(entry: domain.IngestResult) -> Optional[str]:
    """Used when close_risk_event is reached but the decision didn't name an
    explicit newStatusCode - falls back to the plain CLOSED status."""
    return domain.master_id_for_code(entry, "STATUS", "CLOSED")
