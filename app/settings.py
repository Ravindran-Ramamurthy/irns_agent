import os


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


SERVER_BASE_URL = _env("SERVER_BASE_URL", "http://localhost:8080").rstrip("/")

# Purely a label this agent puts on its own /agent/prompts and /agent/llm-call
# calls - unlike chat-agent/ivr-agent, irns-agent is never proxied through
# AgentRouteFilter/agent.routes[n] (smartgateway calls it directly, not the
# other way round), so there is no routes[n].name it needs to match.
AGENT_ID = _env("AGENT_ID", "irns-agent")

GROUP_NAME = _env("GROUP_NAME", "bksystems")
PRODUCT_NAME = _env("PRODUCT_NAME", "bksai")
BRAND_NAME = _env("BRAND_NAME", "swiftresolve")

AGENT_PROMPTS_PATH = _env("AGENT_PROMPTS_PATH", "/agent/prompts")
AGENT_PROMPT_PATH = _env("AGENT_PROMPT_PATH", "/agent/prompt")
LLM_CALL_PATH = _env("LLM_CALL_PATH", "/agent/llm-call")

# Where this agent creates the action event it decides on, and updates the
# risk event's status if the decision calls for it - both plain REST calls
# back into smartgateway-backend-core's ai.smartgateway.backend.irns
# controllers, never a direct DB connection.
ACTION_EVENT_CREATE_PATH = _env("ACTION_EVENT_CREATE_PATH", "/tenant/irns/action-event")
RISK_EVENT_UPDATE_PATH = _env("RISK_EVENT_UPDATE_PATH", "/tenant/irns/risk-event")

# Read-only lookups the /actionevent lane uses to reassemble a risk event's
# full history from just the id it's given - the same GET endpoints the UI
# itself calls, never a direct DB read.
RISK_EVENT_GET_PATH_TEMPLATE = _env("RISK_EVENT_GET_PATH_TEMPLATE", "/tenant/irns/risk-event/{id}")
ACTION_EVENT_LIST_PATH_TEMPLATE = _env(
    "ACTION_EVENT_LIST_PATH_TEMPLATE", "/tenant/irns/action-event/by-risk-event/{riskEventId}")
INCOMING_EVENT_LIST_PATH_TEMPLATE = _env(
    "INCOMING_EVENT_LIST_PATH_TEMPLATE", "/tenant/irns/incoming-event/by-risk-event/{riskEventId}")

# {type} is one of IrnsMasterType's names: ALERT_CODE, ACTION_TYPE,
# ACTION_STATUS, STATUS - see app/domain.py's ingest().
MASTER_ALL_PATH_TEMPLATE = _env("MASTER_ALL_PATH_TEMPLATE", "/tenant/irns/master/{type}/all")

# Single-row lookup by id, used for ALERT_CODE/RESPONSE_CODE - a risk/incoming
# event only ever needs the one row its own FK points at, not the whole
# table - see app/domain.py's load_alert_code()/load_response_code().
MASTER_BY_ID_PATH_TEMPLATE = _env("MASTER_BY_ID_PATH_TEMPLATE", "/tenant/irns/master/{type}/{id}")

SERVER_TIMEOUT_SECONDS = float(_env("SERVER_TIMEOUT_SECONDS", "30"))

# "llm" - fetch WORKFLOW_INSTRUCTIONS/RESPONSE_CODE_INSTRUCTIONS, a flexible
# natural-language description a tenant can phrase however they like, for
# an LLM to interpret. "local" - fetch WORKFLOW_JSON/RESPONSE_CODE_JSON, the
# strict machine-parseable schedule, for LocalDecider to parse without any
# LLM call. Both keys hold the same underlying workflow, just in a different
# shape - see PromptScopeRegistry's "irns-agent"|"AlertCode"/"ResponseCode"
# scopes on the Java side. app/decider.py's build_decider() picks the Decider
# class per mode; each hardcodes the key it wants when it calls
# app/domain.py's load_alert_code()/load_response_code() per event
# (LlmDecider via app/reasoning.py, LocalDecider directly).
#
# This is only the process-wide default for a tenant with no override of its
# own - see app/store.py's get_decider_mode()/set_decider_mode(), which are
# per-tenant and runtime-mutable (POST /decider-mode in app/main.py), unlike
# this env-derived constant.
DECIDER_MODE = _env("DECIDER_MODE", "llm")

VALID_DECIDER_MODES = ("llm", "local")

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
