# AI Orchestrator — FastAPI service that runs the agent loop, session memory,
# and PII write-back tools. Talks to GMS, MySQL, and the DataHub MCP server.
#
#   docker build -t datahub-ai-orchestrator ./ai-orchestrator
FROM python:3.13-slim

WORKDIR /app

# Install deps first so image layers cache when only source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source (only what the service needs at runtime).
COPY *.py ./
COPY scripts ./scripts
COPY bootstrap ./bootstrap

# 0.0.0.0 binds inside the container; compose restricts the published port.
ENV DATAHUB_TELEMETRY_ENABLED=false

EXPOSE 8000

# Run as non-root for safety.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# JSON exec form so uvicorn is PID 1 and receives SIGTERM for graceful shutdown.
ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
