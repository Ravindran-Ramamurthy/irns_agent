import hashlib
import logging
import re
from typing import Any, Dict, Optional

import yaml

from app import settings

log = logging.getLogger(__name__)

_prompts: Dict[str, Any] = {}
_fingerprint: str = "unloaded"

# This module's own lowercase keys -> the server's PromptKey enum name used
# to look up a tenant override (see server_client.get_prompts()). Kept in
# sync with PromptScopeRegistry's "irns-agent"|"Shared" block and
# AgentSupportController.AGENT_PROMPT_KEYS on the Java side - a key simply
# not being here is what would keep it local-only, though every key this
# agent has is tenant-overridable.
_OVERRIDE_KEY_NAMES = {
    "risk_event_system": "RISK_EVENT_SYSTEM",
    "risk_event_context": "RISK_EVENT_CONTEXT",
    "incoming_event_system": "INCOMING_EVENT_SYSTEM",
    "incoming_event_context": "INCOMING_EVENT_CONTEXT",
    "action_event_system": "ACTION_EVENT_SYSTEM",
    "action_event_context": "ACTION_EVENT_CONTEXT",
}


def load() -> None:
    global _prompts, _fingerprint

    path = settings.PROMPTS_PATH
    if not path.exists():
        log.warning("No prompt file at %s - falling back to the packaged defaults at %s",
                    path, settings.PROMPTS_FALLBACK_PATH)
        path = settings.PROMPTS_FALLBACK_PATH

    raw = path.read_bytes()
    _prompts = yaml.safe_load(raw) or {}
    _fingerprint = hashlib.sha256(raw).hexdigest()[:12]

    log.info("Loaded prompts from %s (fingerprint %s)", path, _fingerprint)


def fingerprint() -> str:
    return _fingerprint


def get(key: str, overrides: Optional[Dict[str, str]] = None) -> str:
    if overrides:
        override_key = _OVERRIDE_KEY_NAMES.get(key)
        if override_key and overrides.get(override_key):
            return str(overrides[override_key])

    shared = _prompts.get("shared") or {}
    if key in shared:
        return str(shared[key])

    raise KeyError(f"No prompt '{key}' in {settings.PROMPTS_PATH}")


def get_or(key: str, default: str, overrides: Optional[Dict[str, str]] = None) -> str:
    try:
        return get(key, overrides)
    except KeyError:
        return default


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(key: str, overrides: Optional[Dict[str, str]] = None, **values: Any) -> str:
    template = get(key, overrides)
    return _PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), match.group(0))), template)
