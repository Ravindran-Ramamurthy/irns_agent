from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class IrnsState(TypedDict, total=False):
    source: str                                # "riskevent" | "incomingevent" | "actionevent"
    tenant_key: str

    risk_event: Dict[str, Any]                 # for "actionevent", starts as just {"id": ...} until gather_action_event refetches it
    incoming_event: Optional[Dict[str, Any]]   # "incomingevent" lane only
    action_events: List[Dict[str, Any]]        # "actionevent"/"incomingevent" lanes - full history
    incoming_events: List[Dict[str, Any]]      # "actionevent"/"incomingevent" lanes - full history

    entry: Any                                 # app.domain.IngestResult - not serialized, no checkpointer in this graph

    decision: Optional[Dict[str, Any]]
    action_event: Optional[Dict[str, Any]]     # created action_event, once record_action runs
    risk_event_updated: Optional[Dict[str, Any]]
    usage: Optional[Dict[str, int]]
    error: Optional[str]


def new_state(source: str,
              tenant_key: str,
              risk_event: Dict[str, Any],
              incoming_event: Optional[Dict[str, Any]] = None) -> IrnsState:
    return IrnsState(
        source=source,
        tenant_key=tenant_key,
        risk_event=risk_event,
        incoming_event=incoming_event,
        action_events=[],
        incoming_events=[],
        entry=None,
        decision=None,
        action_event=None,
        risk_event_updated=None,
        usage=None,
        error=None,
    )
