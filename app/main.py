import logging
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app import graph as graph_module, graph_runner, settings, store
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
    graph_module.build()
    _http = httpx.AsyncClient(timeout=httpx.Timeout(settings.SERVER_TIMEOUT_SECONDS))
    log.info("irns agent ready. Server at %s (each event loads ACTION_TYPE/ACTION_STATUS/"
              "STATUS live, no cache - see /riskevent, /incomingevent, /actionevent)",
              settings.SERVER_BASE_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _http:
        await _http.aclose()


@app.get("/health")
async def health() -> Dict[str, object]:
    return {"status": "UP"}


@app.get("/buildinfo")
async def buildinfo() -> Dict[str, object]:
    return {"group": settings.GROUP_NAME, "product": settings.PRODUCT_NAME, "brand": settings.BRAND_NAME}


@app.get("/decider-mode")
async def decider_mode(x_tenant_id: str = Header(default="")) -> Dict[str, Any]:
    """This tenant's current decider mode ("llm"|"local") - see
    app/store.py's get_decider_mode(). Read-only; POST to change it."""
    return {"mode": store.get_decider_mode(x_tenant_id)}


@app.post("/decider-mode")
async def set_decider_mode(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Sets (or, with no body/mode, toggles) this tenant's decider mode -
    runtime-mutable, no restart needed, e.g. to demo both llm and local
    against the same tenant. Takes effect on the very next event - there is
    no cache to invalidate any more (see app/store.py)."""
    headers = forwardable(request.headers)
    if not _has_credential(headers):
        return _problem(401, "No session")

    body = await _body(request)
    mode = body.get("mode")
    try:
        if mode:
            new_mode = store.set_decider_mode(x_tenant_id, mode)
        else:
            new_mode = store.toggle_decider_mode(x_tenant_id)
    except ValueError as exc:
        return _problem(400, str(exc))

    return JSONResponse(status_code=200, content={"mode": new_mode})


@app.post("/riskevent")
async def risk_event_created(request: Request, x_tenant_id: str = Header(default="")) -> JSONResponse:
    """Called by smartgateway's RiskEventController right after a risk event
    is created (fire-and-forget - the browser's create request has already
    returned by the time this runs). Runs the "riskevent" lane of the graph
    (see app/graph.py): loads this tenant's IRNS master data fresh, decides
    the next action via one LLM call, and records it."""
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
