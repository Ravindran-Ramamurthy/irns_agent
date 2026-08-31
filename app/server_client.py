import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app import settings

log = logging.getLogger(__name__)

SESSION_ID_HEADER = "X-Session-Id"
REQUEST_ID_HEADER = "X-Request-Id"

_NOT_REPLAYED = {
    "host",
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "upgrade",
    "te",
    "trailer",
    "accept",
    "accept-encoding",
    # Describes the ORIGINAL browser request's body (see smartgateway's
    # SessionPayloadCryptoFilter) - never true of anything this agent itself
    # sends, which is always plain JSON. IrnsAgentClient already strips this
    # before calling in; stripped again here in case it ever isn't.
    "x-encrypted",
}


def forwardable(headers) -> Dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _NOT_REPLAYED}


class ServerError(Exception):

    def __init__(self, message: str, statuses: Optional[List[str]] = None):
        super().__init__(message)
        self.statuses: List[str] = statuses or []


class ServerClient:
    """Every read and write this agent needs goes through smartgateway's own
    REST controllers via this client - it never opens a Postgres/OpenSearch
    connection itself. Same shape as chat_agent/ivr_agent's ServerClient,
    plus the 3 IRNS-specific calls this agent needs to act on its decision
    (create_action_event/update_risk_event/get_master_all)."""

    def __init__(self, client: httpx.AsyncClient, headers: Dict[str, str]):
        self._http = client
        # Plain application/json, NOT also x-ndjson - see llm-call's own
        # Accept override in _post(), same reasoning as ivr_agent's client.
        self._headers = {**headers, "Accept": "application/json"}

    async def get_prompts(self, sub_agent: str) -> Dict[str, str]:
        """Tenant-configured prompt overrides. Never raises: any failure just
        means no overrides, falling back to the local prompts.yaml."""
        url = settings.SERVER_BASE_URL + settings.AGENT_PROMPTS_PATH
        params = {"agent": settings.AGENT_ID, "subAgent": sub_agent}
        try:
            response = await self._http.get(url, headers=self._headers, params=params)
        except httpx.HTTPError as exc:
            log.warning("Could not fetch prompt overrides: %s", exc)
            return {}
        if response.status_code != 200:
            log.warning("%s returned HTTP %s fetching prompt overrides", url, response.status_code)
            return {}
        try:
            body = response.json()
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    async def list_domains(self) -> List[Dict[str, Any]]:
        """All (non-deleted) knowledge domains visible to this tenant."""
        url = settings.SERVER_BASE_URL + settings.DOMAIN_LIST_PATH
        params = {"page": 0, "size": 200, "sortBy": "name", "sortDir": "asc", "deleted": "false"}
        response = await self._get(url, params)
        body = _json_or_raise(response, "list domains")
        return body if isinstance(body, list) else []

    async def list_domain_files(self, domain_id: str) -> List[Dict[str, Any]]:
        url = f"{settings.SERVER_BASE_URL}{settings.DOMAIN_FILES_PATH}/{domain_id}"
        response = await self._get(url, {})
        body = _json_or_raise(response, "list domain files")
        return body if isinstance(body, list) else []

    async def download_file(self, file_id: str) -> bytes:
        url = f"{settings.SERVER_BASE_URL}{settings.DOMAIN_FILE_DOWNLOAD_PATH}/{file_id}"
        response = await self._get(url, {})
        if response.status_code == 401:
            raise ServerError("session expired")
        if response.status_code >= 400:
            raise ServerError(f"download file: HTTP {response.status_code}")
        return response.content

    async def get_master_all(self, master_type: str) -> List[Dict[str, Any]]:
        """All active rows of one IRNS lookup table (ACTION_TYPE, ACTION_STATUS
        or STATUS) - used to map the LLM's human-readable codes back to the FK
        ids ActionEvent/RiskEvent actually need."""
        url = settings.SERVER_BASE_URL + settings.MASTER_ALL_PATH_TEMPLATE.format(type=master_type)
        response = await self._get(url, {})
        body = _json_or_raise(response, f"list master {master_type}")
        return body if isinstance(body, list) else []

    async def create_action_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = settings.SERVER_BASE_URL + settings.ACTION_EVENT_CREATE_PATH
        response = await self._post(url, payload, accept="application/json")
        return _json_or_raise(response, "create action event")

    async def update_risk_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = settings.SERVER_BASE_URL + settings.RISK_EVENT_UPDATE_PATH
        response = await self._put(url, payload)
        return _json_or_raise(response, "update risk event")

    async def call_llm(self, agent: str, system_prompt: str, context: str, query: str) -> Tuple[str, List[str], Optional[Dict[str, Any]]]:
        """Same endpoint/NDJSON contract chat_agent/ivr_agent's call_llm()
        uses. This agent makes at most one such call per event, so no
        cross-call usage accumulator is needed."""
        url = settings.SERVER_BASE_URL + settings.LLM_CALL_PATH
        payload = {"agent": agent, "systemPrompt": system_prompt, "context": context, "query": query}

        all_statuses: List[str] = []
        attempts = 2
        for attempt in range(attempts):
            response = await self._post(url, payload, accept="application/x-ndjson, application/json")
            text, usage, statuses = _text_from_ndjson(response, "llm-call")
            all_statuses.extend(statuses)
            if text:
                return text, all_statuses, usage
            if attempt + 1 < attempts:
                log.warning("llm-call returned no text (attempt %d/%d) - retrying", attempt + 1, attempts)
                all_statuses.append("The model returned an empty response - retrying")

        raise ServerError("llm-call: no text in the response", statuses=all_statuses)

    async def _get(self, url: str, params: Dict[str, Any]) -> httpx.Response:
        try:
            return await self._http.get(url, headers=self._headers, params=params)
        except httpx.TimeoutException as exc:
            raise ServerError(f"timed out calling {url}") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"could not reach {url}: {exc}") from exc

    async def _post(self, url: str, payload: Dict[str, Any], accept: str) -> httpx.Response:
        headers = {**self._headers, "Content-Type": "application/json", "Accept": accept}
        try:
            return await self._http.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ServerError(f"timed out calling {url}") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"could not reach {url}: {exc}") from exc

    async def _put(self, url: str, payload: Dict[str, Any]) -> httpx.Response:
        headers = {**self._headers, "Content-Type": "application/json"}
        try:
            return await self._http.put(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ServerError(f"timed out calling {url}") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"could not reach {url}: {exc}") from exc


def _text_from_ndjson(response: httpx.Response, what: str) -> Tuple[str, Optional[Dict[str, Any]], List[str]]:
    if response.status_code == 401:
        raise ServerError(f"{what}: session expired")
    if response.status_code >= 400:
        log.error("%s failed: HTTP %s %s", what, response.status_code, response.text[:500])
        raise ServerError(f"{what}: HTTP {response.status_code}")

    deltas: List[str] = []
    completed = ""
    usage: Optional[Dict[str, Any]] = None
    statuses: List[str] = []

    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        value = ((event.get("content") or {}) or {}).get("value") or ""

        if kind == "DELTA":
            deltas.append(value)
        elif kind == "COMPLETE":
            if value:
                completed = value
            if event.get("usage"):
                usage = event["usage"]
        elif kind == "STATUS":
            if value:
                statuses.append(value)
        elif kind == "ERROR":
            raise ServerError(f"{what}: {value or 'the server reported an error'}", statuses=statuses)

    return (completed or "".join(deltas)).strip(), usage, statuses


def _json_or_raise(response: httpx.Response, what: str) -> Any:
    if response.status_code == 401:
        raise ServerError(f"{what}: session expired")
    if response.status_code >= 400:
        log.error("%s failed: HTTP %s %s", what, response.status_code, response.text[:500])
        raise ServerError(f"{what}: HTTP {response.status_code}")
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise ServerError(f"{what}: response was not JSON") from exc
