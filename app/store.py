"""Per-tenant ingested state - mirrors ivr_agent/app/store.py exactly (keyed
by the x_tenant_id header every call into this agent already carries, same
as ivr_agent's own tenant-scoped extension cache). ensure_loaded() lazily
ingests on first use per tenant (the "init on its own when loaded" behavior -
using that same request's own forwarded session, no separate auth needed);
reload() always re-ingests (used by POST /init, for when the tenant's
document has changed); status() only peeks at whatever is already cached,
for a cheap UI status check that never triggers ingestion work itself."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.domain import DomainError, ingest
from app.server_client import ServerClient, ServerError

log = logging.getLogger(__name__)


@dataclass
class Entry:
    chunks: list
    masters: Dict[str, Dict[str, Dict[str, Any]]]
    error: Optional[str]
    ingested_at: Optional[datetime]

    @property
    def ingested(self) -> bool:
        return self.error is None and bool(self.chunks)


# A single-tenant deployment with no tenant header just uses "".
_cache: Dict[str, Entry] = {}


async def ensure_loaded(client: ServerClient, tenant_key: str) -> Entry:
    entry = _cache.get(tenant_key)
    if entry is None:
        entry = await _load(client, tenant_key)
    return entry


async def reload(client: ServerClient, tenant_key: str) -> Entry:
    return await _load(client, tenant_key)


def status(tenant_key: str) -> Optional[Entry]:
    return _cache.get(tenant_key)


def reset(tenant_key: str) -> None:
    """Evicts this tenant's cached entry - called on POST /resetContext, so
    the next ensure_loaded() for this tenant re-ingests from scratch instead
    of serving stale chunks/masters. Safe to call even if nothing is
    cached."""
    _cache.pop(tenant_key, None)


async def _load(client: ServerClient, tenant_key: str) -> Entry:
    try:
        result = await ingest(client)
        entry = Entry(chunks=result.chunks, masters=result.masters, error=None,
                      ingested_at=datetime.now(timezone.utc))
        log.info("Ingested %d chunk(s) for tenant %r", len(result.chunks), tenant_key)
    except DomainError as exc:
        entry = Entry(chunks=[], masters={}, error=str(exc), ingested_at=None)
        log.warning("IRNS domain error for tenant %r: %s", tenant_key, exc)
    except ServerError as exc:
        if "session expired" in str(exc):
            message = "Session expired while ingesting the IRNS domain - please sign in again and retry."
        else:
            message = "Could not reach the server to ingest the IRNS knowledge domain."
        entry = Entry(chunks=[], masters={}, error=message, ingested_at=None)
        log.warning("IRNS domain load failed for tenant %r: %s", tenant_key, exc)

    _cache[tenant_key] = entry
    return entry


def master_id_for_code(entry: Entry, master_type: str, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    row = entry.masters.get(master_type, {}).get(code)
    return row.get("id") if row else None
