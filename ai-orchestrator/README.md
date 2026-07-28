# DataHub AI Orchestrator

Small Python service that powers the DataHub AI Assistant chat. It runs a
**model-agnostic agent loop**: takes a user question + page context, lets the LLM
(Claude) call **DataHub tools** (GraphQL) to fetch real metadata, and streams the
answer back to the UI over SSE.

> Hackathon build. Target is to port this loop into GMS (Java). See SHIPIT-64.

## Architecture

```
Browser (AIChatButton) --POST /api/ai/chat--> Orchestrator (this) --> Claude API
                        <----- SSE tokens -----            |
                                                           v
                                    DataHub MCP server (container) --> DataHub GMS
```

The MCP session is opened **once** at startup and reused for every request
(`PersistentMCP` in `mcp_tools.py`), so no server is spawned per chat message.
A background task owns the session and auto-reconnects if it drops.

Files:

- `main.py` — FastAPI server, SSE endpoint, config endpoints, MCP lifespan
- `agent.py` — model-agnostic agent loop (tool-use cycle)
- `mcp_tools.py` — persistent MCP session, tool discovery and execution
- `mcp-server/Dockerfile` — standalone MCP server image (pinned version)
- `docker-compose.mcp.yml` — runs that MCP server on `127.0.0.1:8001`
- `datahub_tools.py` — legacy direct-GraphQL tools, kept for reference only

## Run locally

Prerequisite: a running DataHub — `scripts/dev/datahub-dev.sh start`.

### 1. Secrets

Create `.env` (gitignored):

```bash
ANTHROPIC_API_KEY=sk-ant-...
DATAHUB_GMS_TOKEN=<pat>                     # required when GMS auth is enabled
DATAHUB_TELEMETRY_ENABLED=false             # avoids slow telemetry retries
DATAHUB_MCP_URL=http://localhost:8001/mcp   # use the containerised MCP server
```

Quickstart GMS runs with `METADATA_SERVICE_AUTH_ENABLED=true`, so a Personal Access
Token is needed. Generate one from the UI: Settings → Access Tokens.

### 2. MCP server

```bash
docker compose -f docker-compose.mcp.yml up -d --build
docker compose -f docker-compose.mcp.yml ps   # expect healthy
```

Published on loopback only — the endpoint carries a DataHub token and the OSS server
does not authenticate callers itself. Bump the pinned server version via
`MCP_SERVER_DATAHUB_VERSION` in `mcp-server/Dockerfile`.

### 3. Orchestrator

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn main:app --port 8000 --reload
```

Startup logs a single `MCP connected (N tools).` line.

## Test

```bash
curl -N -X POST http://localhost:8000/api/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What PII fields does the users dataset have?","context":{"pageType":"home"}}'
```

The UI (`AIChatButton.tsx`) points at `http://localhost:8000/api/ai/chat` and falls
back to a mock if the orchestrator is not running.
