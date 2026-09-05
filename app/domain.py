import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.server_client import ServerClient

log = logging.getLogger(__name__)

# The 3 lookup tables every decision needs in full: every decide() call
# embeds the complete ACTION_TYPE/STATUS code list into the LLM's context
# ("AVAILABLE ACTION TYPES: ...") so it knows what's valid to choose, and
# ACTION_STATUS has no "get by code" endpoint to fetch its one needed row
# ("PENDING") individually. Fetched fresh every single event (see
# app/graph.py's _load_domain) - no cross-event cache, no Re-init: an edited
# row is picked up on the very next event. ALERT_CODE/RESPONSE_CODE are not
# here at all - see load_alert_code()/load_response_code() below - a risk/
# incoming event only ever needs the one row its own FK id points at, not
# the whole table.
_MASTER_TYPES = ("ACTION_TYPE", "ACTION_STATUS", "STATUS")


class DomainError(Exception):
    """Anything wrong with the IRNS knowledge domain itself - no master data
    reachable, or no statuses configured at all. Distinct from ServerError
    (network/HTTP failure talking to smartgateway) so callers can tell
    "smartgateway is down" apart from "the tenant hasn't set IRNS up"."""


@dataclass(frozen=True)
class IngestResult:
    masters: Dict[str, Dict[str, Dict[str, Any]]]  # master type -> code -> row


async def ingest(client: ServerClient) -> IngestResult:
    """Loads the 3 IRNS master tables every decision needs in full, live -
    called once per event (see app/graph.py's _load_domain), never cached
    across events. Raises DomainError if there is no status configured at
    all (nothing to set a risk event to); ServerError if smartgateway itself
    couldn't be reached."""
    masters: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for master_type in _MASTER_TYPES:
        rows = await client.get_master_all(master_type)
        masters[master_type] = {row["code"]: row for row in rows if row.get("code")}

    if not masters.get("STATUS"):
        raise DomainError("No IRNS statuses are configured for this tenant")

    return IngestResult(masters=masters)


def master_id_for_code(result: IngestResult, master_type: str, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    row = result.masters.get(master_type, {}).get(code)
    return row.get("id") if row else None


async def load_alert_code(
        client: ServerClient, alert_code_id: Optional[str], workflow_key: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Live per-event replacement for the old bulk ALERT_CODE preload: fetches
    just the one row a risk/incoming/action event's own alertCodeId points
    at, plus its prompt template. Both the row and its instructions are
    looked up fresh every call - no caching, so an edited row or prompt is
    picked up on the very next event with no Re-init needed.

    workflow_key: "WORKFLOW_INSTRUCTIONS" (prose, for LlmDecider) or
    "WORKFLOW_JSON" (strict, for LocalDecider) - the caller decides which,
    same as the old settings.DECIDER_MODE-driven choice.

    Returns (None, None) if alert_code_id is falsy or no longer resolves to a
    row - the caller's own fallback text handles that, not this function."""
    if not alert_code_id:
        return None, None
    row = await client.get_master_by_id("ALERT_CODE", alert_code_id)
    if not row:
        return None, None
    instructions = await client.get_prompt_template("AlertCode", row.get("code"), workflow_key)
    return row, instructions


async def load_response_code(
        client: ServerClient, response_code_id: Optional[str], response_key: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Same as load_alert_code(), for an incoming event's responseCodeId."""
    if not response_code_id:
        return None, None
    row = await client.get_master_by_id("RESPONSE_CODE", response_code_id)
    if not row:
        return None, None
    instructions = await client.get_prompt_template("ResponseCode", row.get("code"), response_key)
    return row, instructions
