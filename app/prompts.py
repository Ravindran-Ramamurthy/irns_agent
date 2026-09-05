import re
from typing import Any, Dict, Optional

# This module's own lowercase keys -> the server's PromptKey enum name used
# to look up a tenant override (see server_client.get_prompts()). Kept in
# sync with PromptScopeRegistry's "irns-agent"|"Shared" block and
# AgentSupportController.AGENT_PROMPT_KEYS on the Java side.
#
# There is no local fallback here (no packaged prompts.yaml, no hardcoded
# constant) - Java is the sole source of default prompt text
# (PromptScopeRegistry's hardcoded defaults, consulted when a prompt's first
# version is created). If a key isn't resolvable server-side yet, get()
# raises - callers surface that as a run-skipping error rather than silently
# substituting local text.
_OVERRIDE_KEY_NAMES = {
    "risk_event_system": "RISK_EVENT_SYSTEM",
    "risk_event_context": "RISK_EVENT_CONTEXT",
    "incoming_event_system": "INCOMING_EVENT_SYSTEM",
    "incoming_event_context": "INCOMING_EVENT_CONTEXT",
    "action_event_system": "ACTION_EVENT_SYSTEM",
    "action_event_context": "ACTION_EVENT_CONTEXT",
}


def get(key: str, overrides: Optional[Dict[str, str]] = None) -> str:
    override_key = _OVERRIDE_KEY_NAMES[key]
    value = (overrides or {}).get(override_key)
    if not value:
        raise KeyError(
            f"No '{override_key}' prompt is configured for irns-agent yet - "
            f"set one via the IRNS Prompts page")
    return str(value)


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(key: str, overrides: Optional[Dict[str, str]] = None, **values: Any) -> str:
    template = get(key, overrides)
    return _PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), match.group(0))), template)
