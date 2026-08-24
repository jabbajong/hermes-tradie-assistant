#!/usr/bin/env sh
set -eu

python /opt/hermes-agents/tradie-assistant/workspace/scripts/prepare_config.py \
  --template /opt/hermes-agents/tradie-assistant/config.template.yaml \
  --output /opt/hermes-agents/tradie-assistant/config.yaml

exec /usr/local/lib/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
