import os
from pathlib import Path


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

DOMAIN_LIST_PATH = _env("DOMAIN_LIST_PATH", "/tenant/domain")
DOMAIN_FILES_PATH = _env("DOMAIN_FILES_PATH", "/tenant/domain/file/domain")
DOMAIN_FILE_DOWNLOAD_PATH = _env("DOMAIN_FILE_DOWNLOAD_PATH", "/tenant/domain/file/download")
AGENT_PROMPTS_PATH = _env("AGENT_PROMPTS_PATH", "/agent/prompts")
LLM_CALL_PATH = _env("LLM_CALL_PATH", "/agent/llm-call")

# Where this agent creates the action event it decides on, and updates the
# risk event's status if the decision calls for it - both plain REST calls
# back into smartgateway-backend-core's ai.smartgateway.backend.irns
# controllers, never a direct DB connection.
ACTION_EVENT_CREATE_PATH = _env("ACTION_EVENT_CREATE_PATH", "/tenant/irns/action-event")
RISK_EVENT_UPDATE_PATH = _env("RISK_EVENT_UPDATE_PATH", "/tenant/irns/risk-event")

# {type} is one of IrnsMasterType's names: ACTION_TYPE, ACTION_STATUS, STATUS
# (the only 3 this agent needs - see app/domain.py's load_masters()).
MASTER_ALL_PATH_TEMPLATE = _env("MASTER_ALL_PATH_TEMPLATE", "/tenant/irns/master/{type}/all")

# Exactly one domain with this name must exist per tenant, containing one or
# more prose documents (.txt/.md/.docx) describing IRNS's workflows - see
# app/domain.py.
KNOWLEDGE_DOMAIN_NAME = _env("KNOWLEDGE_DOMAIN_NAME", "IRNS")

# Naive fixed-size chunking, no embeddings/vector store - "simple langchain
# is adequate" per spec. See app/domain.py.
CHUNK_SIZE_CHARS = int(_env("CHUNK_SIZE_CHARS", "1500"))
CHUNK_OVERLAP_CHARS = int(_env("CHUNK_OVERLAP_CHARS", "200"))
# Caps how much of the ingested document is actually sent as LLM context per
# decision - a hard budget, not retrieval-ranked (see app/reasoning.py).
MAX_CONTEXT_CHUNKS = int(_env("MAX_CONTEXT_CHUNKS", "12"))

SERVER_TIMEOUT_SECONDS = float(_env("SERVER_TIMEOUT_SECONDS", "30"))

DATA_BASE_PATH = Path(_env("DATA_BASE_PATH", "/data"))
PROMPTS_PATH = Path(_env("PROMPTS_PATH", str(DATA_BASE_PATH / "config" / "prompts.yaml")))
PROMPTS_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts.yaml"

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
