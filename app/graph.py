import logging
from typing import Any, Dict

from langgraph.graph import END, StateGraph

from app import decider as decider_module
from app import domain, reasoning
from app.domain import DomainError
from app.server_client import ServerClient, ServerError
from app.state import IrnsState

log = logging.getLogger(__name__)

_graph = None

# The flow: load_domain -> gather_{risk_event,incoming_event,action_event}
# (which lane, fixed by the trigger that started this run) -> decide (one
# shared LLM call for all 3 lanes) -> record_action | close_risk_event ->
# END. No node here ever calls interrupt() - every run is a single fire-
# and-forget pass, so build() below compiles with no checkpointer at all.


def _client(config: Dict[str, Any]) -> ServerClient:
    return config["configurable"]["client"]


async def _load_domain(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """Loads ACTION_TYPE/ACTION_STATUS/STATUS fresh for this one run - no
    cache, no Re-init: see app/domain.py's ingest()."""
    try:
        entry = await domain.ingest(_client(config))
    except DomainError as exc:
        log.warning("IRNS agent: domain error for tenant %r: %s", state["tenant_key"], exc)
        return {"error": str(exc)}
    except ServerError as exc:
        log.warning("IRNS agent: could not reach server to load the domain: %s", exc)
        return {"error": f"Could not reach the server to load the IRNS domain: {exc}"}

    return {"entry": entry}


def _after_load_domain(state: IrnsState) -> str:
    if state.get("error"):
        return "end_error"
    return state["source"]  # "riskevent" | "incomingevent" | "actionevent"


async def _gather_risk_event(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    # Already in state - the /riskevent POST body carries the full risk
    # event, no extra fetch needed.
    return {}


async def _gather_incoming_event(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """The incoming event and its parent risk event are already in state -
    the /incomingevent POST body carries both. Still fetches the risk
    event's action/incoming-event history, same GET calls
    _gather_action_event uses, since decide_for_incoming_event needs it to
    know current state (e.g. is IB currently blocked right now) - see
    app/decider.py's LocalDecider."""
    client = _client(config)
    risk_event_id = state["risk_event"]["id"]

    try:
        action_events = await client.get_action_events(risk_event_id)
        incoming_events = await client.get_incoming_events(risk_event_id)
    except ServerError as exc:
        log.warning("IRNS agent: could not load history for risk event %s: %s", risk_event_id, exc)
        return {"error": f"Could not load risk event history: {exc}"}

    return {"action_events": action_events, "incoming_events": incoming_events}


async def _gather_action_event(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """/actionevent only carries a risk event id - fetch everything else
    needed to re-review it via the same GET endpoints the UI itself uses."""
    client = _client(config)
    risk_event_id = state["risk_event"]["id"]

    try:
        risk_event = await client.get_risk_event(risk_event_id)
        action_events = await client.get_action_events(risk_event_id)
        incoming_events = await client.get_incoming_events(risk_event_id)
    except ServerError as exc:
        log.warning("IRNS agent: could not load history for risk event %s: %s", risk_event_id, exc)
        return {"error": f"Could not load risk event history: {exc}"}

    return {"risk_event": risk_event, "action_events": action_events, "incoming_events": incoming_events}


async def _decide(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """Picks the Decider (LlmDecider|LocalDecider) per settings.DECIDER_MODE
    and calls whichever of its 3 methods matches this run's lane - see
    app/decider.py. Everything downstream (record_action/close_risk_event)
    only ever sees the resulting Decision dict, identically either way."""
    decider = decider_module.build_decider(_client(config), state["tenant_key"])
    entry = state["entry"]
    risk_event = state["risk_event"]
    action_events = state.get("action_events") or []
    incoming_events = state.get("incoming_events") or []
    source = state["source"]

    try:
        if source == "riskevent":
            decision, usage = await decider.decide_for_risk_event(entry, risk_event)
        elif source == "incomingevent":
            decision, usage = await decider.decide_for_incoming_event(
                entry, risk_event, action_events, incoming_events, state["incoming_event"])
        else:
            action_event = action_events[-1] if action_events else {}
            decision, usage = await decider.decide_for_action_event(
                entry, action_event, risk_event, action_events, incoming_events)
    except ServerError as exc:
        log.warning("IRNS agent: decision call failed: %s", exc)
        return {"error": f"Could not reach the server for a decision: {exc}"}

    if not decision:
        return {"error": "No usable decision was produced"}
    return {"decision": decision, "usage": usage}


def _after_decide(state: IrnsState) -> str:
    if state.get("error"):
        return "end_error"
    decision = state.get("decision") or {}
    if decision.get("actionType") or decision.get("reviewAt"):
        return "record_action"
    return "close_risk_event"


async def _record_action(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """Always an insert - even a "nothing to do yet" decision is its own
    action_event row (see reasoning.build_action_event_payload), never a
    mutation of the prior one. Also carries reviewAt onto the risk event
    itself so the scheduler's poll query stays a single-table scan."""
    client = _client(config)
    entry = state["entry"]
    decision = state["decision"]
    risk_event = state["risk_event"]

    try:
        payload = reasoning.build_action_event_payload(entry, risk_event["id"], decision)
        created = await client.create_action_event(payload)

        updated = dict(risk_event)
        updated["reviewAt"] = decision.get("reviewAt")
        new_status_id = reasoning.resolve_new_status_id(entry, decision)
        if new_status_id:
            updated["statusId"] = new_status_id
        updated_risk_event = await client.update_risk_event(updated)
    except ServerError as exc:
        log.warning("IRNS agent: could not record the decision for risk event %s: %s", risk_event.get("id"), exc)
        return {"error": f"Could not record the decision: {exc}"}

    return {"action_event": created, "risk_event_updated": updated_risk_event}


async def _close_risk_event(state: IrnsState, config: Dict[str, Any]) -> Dict[str, Any]:
    """Reached only when the decision has neither an actionType nor a
    reviewAt - nothing left to do and no future review point, so this risk
    event is done."""
    client = _client(config)
    entry = state["entry"]
    decision = state["decision"]
    risk_event = state["risk_event"]

    new_status_id = reasoning.resolve_new_status_id(entry, decision) or reasoning.default_closed_status_id(entry)
    updated = dict(risk_event)
    updated["statusId"] = new_status_id
    updated["reviewAt"] = None

    try:
        updated_risk_event = await client.update_risk_event(updated)
    except ServerError as exc:
        log.warning("IRNS agent: could not close risk event %s: %s", risk_event.get("id"), exc)
        return {"error": f"Could not close the risk event: {exc}"}

    return {"risk_event_updated": updated_risk_event}


def build() -> None:
    global _graph

    builder = StateGraph(IrnsState)
    builder.add_node("load_domain", _load_domain)
    builder.add_node("gather_risk_event", _gather_risk_event)
    builder.add_node("gather_incoming_event", _gather_incoming_event)
    builder.add_node("gather_action_event", _gather_action_event)
    builder.add_node("decide", _decide)
    builder.add_node("record_action", _record_action)
    builder.add_node("close_risk_event", _close_risk_event)

    builder.set_entry_point("load_domain")
    builder.add_conditional_edges("load_domain", _after_load_domain, {
        "riskevent": "gather_risk_event",
        "incomingevent": "gather_incoming_event",
        "actionevent": "gather_action_event",
        "end_error": END,
    })
    builder.add_edge("gather_risk_event", "decide")
    builder.add_edge("gather_incoming_event", "decide")
    builder.add_edge("gather_action_event", "decide")
    builder.add_conditional_edges("decide", _after_decide, {
        "record_action": "record_action",
        "close_risk_event": "close_risk_event",
        "end_error": END,
    })
    builder.add_edge("record_action", END)
    builder.add_edge("close_risk_event", END)

    _graph = builder.compile()
    log.info("IRNS graph compiled")


def compiled():
    if _graph is None:
        raise RuntimeError("Graph not built - build() must run at startup")
    return _graph
