import json
import logging
import re
from typing import Any, Dict, Optional

from langchain_core.prompts import PromptTemplate

from app import prompts, settings, store
from app.server_client import ServerClient

log = logging.getLogger(__name__)

# "Simple langchain is adequate" per spec - this agent is a single-shot
# decide-and-act per event, not a multi-turn conversation, so a plain
# PromptTemplate to format the query text is all the orchestration it
# needs. The model call itself still goes through smartgateway's
# /agent/llm-call (see ServerClient.call_llm) - this agent never talks to
# an LLM provider directly, same as chat_agent/ivr_agent.
_RISK_EVENT_QUERY_TEMPLATE = PromptTemplate.from_template("RISK EVENT:\n{risk_event_json}")
_INCOMING_EVENT_QUERY_TEMPLATE = PromptTemplate.from_template(
    "RISK EVENT:\n{risk_event_json}\n\nINCOMING EVENT:\n{incoming_event_json}")

# Last-resort local fallbacks if BOTH the server-side resolution (DB
# override / TenantPropertyResolver / PromptScopeRegistry hardcoded
# default) and the local config/prompts.yaml are somehow unavailable - kept
# in sync with prompts.yaml's own text, though it doesn't have to match
# exactly (same relationship as ivr_agent's graph.py _DEFAULT_* constants).
_FALLBACK_RISK_EVENT_SYSTEM = (
    "You are the IRNS risk-event workflow assistant for a bank. Decide the single "
    "next action IRNS should take for the risk event given to you, replying with "
    "ONLY a JSON object: {\"actionType\":\"...\",\"actionCode\":\"...\","
    "\"channel\":\"...\",\"comments\":\"...\",\"newStatusCode\":\"...\"}."
)
_FALLBACK_INCOMING_EVENT_SYSTEM = (
    "You are the IRNS risk-event workflow assistant for a bank. Decide the single "
    "next action IRNS should take in response to the incoming event given to you, "
    "against its risk event, replying with ONLY a JSON object: "
    "{\"actionType\":\"...\",\"actionCode\":\"...\",\"channel\":\"...\","
    "\"comments\":\"...\",\"newStatusCode\":\"...\"}."
)
def _context_text(entry: store.Entry) -> str:
    return "\n\n".join(entry.chunks[:settings.MAX_CONTEXT_CHUNKS])


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
    return decision if isinstance(decision, dict) else {}


async def decide_risk_event(client: ServerClient, entry: store.Entry, risk_event: Dict[str, Any]) -> Dict[str, Any]:
    overrides = await client.get_prompts("Shared")
    system = prompts.get_or("risk_event_system", _FALLBACK_RISK_EVENT_SYSTEM, overrides)
    context = prompts.render("risk_event_context", overrides, context=_context_text(entry))
    query = _RISK_EVENT_QUERY_TEMPLATE.format(risk_event_json=json.dumps(risk_event, default=str))

    reply, _statuses, _usage = await client.call_llm(settings.AGENT_ID, system, context, query)
    return _parse_decision(reply)


async def decide_incoming_event(
        client: ServerClient, entry: store.Entry, risk_event: Dict[str, Any], incoming_event: Dict[str, Any]) -> Dict[str, Any]:
    overrides = await client.get_prompts("Shared")
    system = prompts.get_or("incoming_event_system", _FALLBACK_INCOMING_EVENT_SYSTEM, overrides)
    context = prompts.render("incoming_event_context", overrides, context=_context_text(entry))
    query = _INCOMING_EVENT_QUERY_TEMPLATE.format(
        risk_event_json=json.dumps(risk_event, default=str),
        incoming_event_json=json.dumps(incoming_event, default=str))

    reply, _statuses, _usage = await client.call_llm(settings.AGENT_ID, system, context, query)
    return _parse_decision(reply)


def build_action_event_payload(entry: store.Entry, risk_event_id: str, decision: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None if the decision's actionType doesn't map to a known
    irns_action_type_master code - nothing sensible to create in that case."""
    action_type_id = store.master_id_for_code(entry, "ACTION_TYPE", decision.get("actionType"))
    if not action_type_id:
        log.warning("Decision actionType %r did not map to a known action type - skipping", decision.get("actionType"))
        return None

    return {
        "riskEventId": risk_event_id,
        "actionTypeId": action_type_id,
        "actionCode": decision.get("actionCode"),
        "channel": decision.get("channel"),
        # This agent's own action events are always recorded as SUCCESS - it
        # is reporting what it did, not something pending/failed.
        "actionStatusId": store.master_id_for_code(entry, "ACTION_STATUS", "SUCCESS"),
        "comments": decision.get("comments"),
    }


def resolve_new_status_id(entry: store.Entry, decision: Dict[str, Any]) -> Optional[str]:
    code = decision.get("newStatusCode")
    return store.master_id_for_code(entry, "STATUS", code) if code else None
