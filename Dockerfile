# syntax=docker/dockerfile:1
# One image for the API (which also serves the built dashboard) and the scheduler (compose.yaml picks the command).
#
# The dashboard is built in a Node stage and served by the API process itself (TRADING_STATIC_DIR, backend/main.py):
# one port, the API on the page's own origin (no CORS, no proxy config), and no second server to keep in step. nginx
# would add a container and a config for a single-operator desk with no static traffic to speak of.
#
# The @bv-ds packages come from a private registry: the build reads ~/.npmrc as a BuildKit secret (compose.yaml
# build.secrets), mounted for `npm ci` only — the token is in no layer of the image.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul TRADING_DATA_DIR=/app/data TRADING_STORE=sqlite TRADING_STATIC_DIR=/app/web
WORKDIR /app
# the market time zones come from the system zone database (zoneinfo)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY agent_trading_core ./agent_trading_core
COPY backend ./backend
# editable: the scheduler loads scripts/daily_loop.py next to the package (agent_trading_core.scheduler.daily_loop)
RUN pip install -e ".[api]"
COPY scripts ./scripts
COPY --from=web /web/dist /app/web
# data/ (records, trading.db, the price cache) is the compose volume, never baked in; .env comes from env_file
VOLUME ["/app/data"]
EXPOSE 8000
# first start (no trading.db yet): load the JSON-lines records and compare both stores' replays — then the API on every
# start (backend/entrypoint.py). compose.yaml's scheduler service runs its own command instead.
CMD ["python", "-m", "backend.entrypoint"]
