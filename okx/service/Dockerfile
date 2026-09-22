# okx/service — the A2MCP endpoint. **This is the only image in the project with pip dependencies**
# (the engine layers store/features/agent remain standard library only). Everything is installed
# inside the image and nothing touches the host.
FROM python:3.12-slim

WORKDIR /app
COPY okx/service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy only what is needed: the engine, the agent and the Dune client.
# ★ Never copy collected data, credentials or live-trading code — none of that belongs on a public
#   server
COPY okx/engine       /app/okx/engine
COPY okx/agent        /app/okx/agent
COPY okx/sql          /app/okx/sql
COPY okx/service      /app/okx/service
COPY dune/lib         /app/dune/lib

# ★ The engine layers have a few defaults hard-coded to /work, while the container root is /app.
#   Both can be overridden by environment variable:
#   OKX_SQL_DIR    where store.py looks for its SQL — unset, the service answers 502
#                  "No such file or directory"
#   DUNE_COST_LOG  the Dune cost log — unset, the write is swallowed by a try/except and **the record
#                  of how many credits each call spent is lost**
ENV OKX_ROOT=/app \
    OKX_SQL_DIR=/app/okx/sql \
    DUNE_COST_LOG=/app/okx/data/_svc/dune_cost.jsonl \
    PYTHONUNBUFFERED=1
EXPOSE 4021
# A single worker: each job runs 2-4 minutes, and more workers would only burn the Dune quota faster.
# Before running concurrently, deal with the quota first (see the README).
CMD ["uvicorn", "okx.service.app:app", "--host", "0.0.0.0", "--port", "4021", \
     "--workers", "1", "--timeout-keep-alive", "310"]
