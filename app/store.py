"""Per-tenant decider mode override - the one piece of IRNS agent state that
genuinely needs to persist across requests (unlike master data, which is now
fetched fresh every event - see app/domain.py/app/graph.py). Runtime-mutable
via POST /decider-mode (see app/main.py) so different tenants - or the same
one, live - can run llm vs local without a restart, e.g. for a demo."""

from typing import Dict

from app import settings

# Absent means "use settings.DECIDER_MODE" (the process-wide default from the
# DECIDER_MODE env var).
_decider_mode: Dict[str, str] = {}


def get_decider_mode(tenant_key: str) -> str:
    return _decider_mode.get(tenant_key, settings.DECIDER_MODE)


def set_decider_mode(tenant_key: str, mode: str) -> str:
    if mode not in settings.VALID_DECIDER_MODES:
        raise ValueError(f"Invalid decider mode {mode!r} - must be one of {settings.VALID_DECIDER_MODES}")
    _decider_mode[tenant_key] = mode
    return mode


def toggle_decider_mode(tenant_key: str) -> str:
    current = get_decider_mode(tenant_key)
    return set_decider_mode(tenant_key, "local" if current == "llm" else "llm")
