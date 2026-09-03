import logging
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import graph as graph_module, graph_runner, prompts, settings, store
from app.server_client import ServerClient, ServerError, forwardable

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
log = logging.getLogger("irns-agent")

app = FastAPI(title="irns agent", docs_url=None, redoc_url=None)

_http: httpx.AsyncClient = None  # type: ignore[assignment]


@app.on_event("startup")
async def _startup() -> None:
    global _http
    prompts.load()
    graph_module.build()
    _http = httpx.AsyncClient(timeout=httpx.Timeout(settings.SERVER_TIMEOUT_SECONDS))
    log.info("irns agent ready. Server at %s, prompts %s (each tenant auto-ingests on first use - "
              "see /riskevent, /incomingevent, /actionevent, /init)", settings.SERVER_BASE_URL, prompts.fingerprint())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http:
        await _http.aclose()


@app.get("/health")
async def health() -> Dict[str, object]:
    return {"status": "UP", "prompts": prompts.fingerprint()}


@app.get("/buildinfo")
async def buildinfo() -> Dict[str, object]:
    return {"group": settings.GROUP_NAME, "product": settings.PRODUCT_NAME, "brand": settings.BRAND_NAME}


def _entry_json(entry: store.Entry) -> Dict[str, Any]:
    return {
        "ingested": entry.ingested,
        "chunkCount": len(entry.chunks),
        "actionTypeCount": len(entry.masters.get("ACTION_TYPE", {})),
        "actionStatusCount": len(entry.masters.get("ACTION_STATUS", {})),
        "statusCount": len(entry.masters.get("STATUS", {})),
        "ingestedAt": entry.ingested_at.isoformat() if entry.ingested_at else None,
        "error": entry.error,
    }


@app.get("/status")
async def status(x_tenant_id: str = Header(default="")) -> Dict[str, Any]:
    """Peeks at whatever is already cached for this tenant - never triggers
    ingestion itself. Powers the UI's status indicator (see
    IrnsAgentStatusController.status() on the Java side)."""
    entry = store.status(x_tenant_id)
    if entry is None:
        return {"ingested": False, "chunkCount": 0, "ingestedAt": None, "error": None}
    return _entry_json(entry)


@app.post("/init")
async def init(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Forces a fresh ingest of this tenant's "IRNS" knowledge domain and
    master lookup tables - the "re-init if the document has changed" button
    on the Java side. Uses this request's own forwarded session, same as
    every other call into this agent - no separate auth for this."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    client = ServerClient(_http, headers)
    entry = await store.reload(client, x_tenant_id)
    return JSONResponse(status_code=200, content=_entry_json(entry))


@app.post("/resetContext")
async def reset_context(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Evicts this tenant's cached IRNS domain (chunks + masters) without
    re-ingesting - the next call that needs it (any of /riskevent,
    /incomingevent, /actionevent, or a direct /init) lazily reloads from
    scratch. Called whenever the tenant's context is reset (e.g. its IRNS
    domain document changed) so a stale cache doesn't linger until someone
    happens to click "re-init"."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    store.reset(x_tenant_id)
    return JSONResponse(status_code=200, content={"reset": True})


@app.post("/riskevent")
async def risk_event_created(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Called by smartgateway's RiskEventController right after a risk event
    is created (fire-and-forget - the browser's create request has already
    returned by the time this runs). Runs the "riskevent" lane of the graph
    (see app/graph.py): auto-ingests this tenant's IRNS domain on first use,
    decides the next action via one LLM call, and records it."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    body = await _body(request)
    risk_event = body.get("riskEvent")
    if not isinstance(risk_event, dict) or not risk_event.get("id"):
        return _problem(400, "riskEvent is required")

    client = ServerClient(_http, headers)
    try:
        result = await graph_runner.run("riskevent", x_tenant_id, client, risk_event=risk_event)
    except ServerError as exc:
        log.warning("riskevent run for %s failed: %s", risk_event.get("id"), exc)
        return _problem(502, f"Could not reach the server: {exc}")

    return JSONResponse(status_code=200, content=result)


@app.post("/incomingevent")
async def incoming_event_created(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Same as /riskevent, but for an incoming event recorded against an
    existing risk event - the parent risk event is included in the request
    body by the Java caller, so this agent needs no extra callback just to
    fetch it. Runs the "incomingevent" lane of the graph."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    body = await _body(request)
    risk_event = body.get("riskEvent")
    incoming_event = body.get("incomingEvent")
    if not isinstance(risk_event, dict) or not isinstance(incoming_event, dict):
        return _problem(400, "riskEvent and incomingEvent are required")

    client = ServerClient(_http, headers)
    try:
        result = await graph_runner.run(
            "incomingevent", x_tenant_id, client, risk_event=risk_event, incoming_event=incoming_event)
    except ServerError as exc:
        log.warning("incomingevent run for risk event %s failed: %s", risk_event.get("id"), exc)
        return _problem(502, f"Could not reach the server: {exc}")

    return JSONResponse(status_code=200, content=result)


@app.post("/actionevent")
async def action_event_due(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Called fire-and-forget once an open risk event's review_at comes due.
    Only a risk event id is required - the "actionevent" lane fetches
    everything else (the risk event itself, its full action history, its
    full incoming-event history) before deciding."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    body = await _body(request)
    risk_event_id = body.get("riskEventId")
    if not risk_event_id:
        return _problem(400, "riskEventId is required")

    client = ServerClient(_http, headers)
    try:
        result = await graph_runner.run("actionevent", x_tenant_id, client, risk_event={"id": risk_event_id})
    except ServerError as exc:
        log.warning("actionevent run for risk event %s failed: %s", risk_event_id, exc)
        return _problem(502, f"Could not reach the server: {exc}")

    return JSONResponse(status_code=200, content=result)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _has_credential(headers: Dict[str, str]) -> bool:
    lowered = {k.lower() for k in headers}
    return "cookie" in lowered or "x-session-id" in lowered


def _problem(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"message": message})
