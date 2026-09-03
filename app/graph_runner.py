import logging
from typing import Any, Dict, Optional

from app import graph as graph_module
from app.server_client import ServerClient
from app.state import new_state

log = logging.getLogger(__name__)


async def run(source: str,
              tenant_key: str,
              client: ServerClient,
              risk_event: Dict[str, Any],
              incoming_event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Runs the graph once, end to end, for one trigger. No streaming/thread
    id needed - nothing in this graph pauses, so a single ainvoke() is
    enough (unlike ivr_agent's astream() + interrupt handling)."""
    state = new_state(source, tenant_key, risk_event, incoming_event)
    config: Dict[str, Any] = {"configurable": {"client": client}}

    result = await graph_module.compiled().ainvoke(state, config=config)

    error = result.get("error")
    if error:
        log.info("IRNS agent: %s run for risk event %s skipped - %s",
                  source, risk_event.get("id"), error)
        return {"skipped": error}

    return {
        "decision": result.get("decision"),
        "actionEvent": result.get("action_event"),
        "riskEvent": result.get("risk_event_updated"),
    }
