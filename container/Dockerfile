FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        procps \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/opt/hermes-agents/tradie-assistant/home \
    HERMES_HOME=/opt/hermes-agents/tradie-assistant \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/hermes-agents/tradie-assistant/workspace/src \
    VIRTUAL_ENV=/usr/local/lib/hermes-agent/venv \
    PATH=/usr/local/lib/hermes-agent/venv/bin:/usr/local/lib/hermes-agent/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/hermes-agents/tradie-assistant

ENTRYPOINT ["/opt/hermes-agents/tradie-assistant/container/entrypoint.sh"]
