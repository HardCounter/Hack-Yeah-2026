FROM node:22-slim AS node

FROM python:3.12-slim
# Node + npm are needed to install the pinned OpenCode CLI (scripts/setup_opencode_pipeline.sh).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
# Windows checkouts can carry CRLF line endings, which break the shebang; strip them first.
RUN sed -i 's/$//' scripts/*.sh && scripts/setup_opencode_pipeline.sh

ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000"]
