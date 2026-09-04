#!/usr/bin/env bash
# Start a reviewed, already deployed Compose stack without rebuilding or replacing it.
set -euo pipefail

: "${UNSTRACT_PROJECT_DIRECTORY:?Set the absolute directory containing the deployed Compose files}"
: "${COMPOSE_FILE:?Set the deployed Compose file list}"
: "${COMPOSE_PROJECT_NAME:?Set the existing Compose project name}"
case "$UNSTRACT_PROJECT_DIRECTORY" in
  /*) ;;
  *) echo "UNSTRACT_PROJECT_DIRECTORY must be absolute" >&2; exit 2 ;;
esac
case "${UNSTRACT_START_TIMEOUT:-300}" in
  ''|*[!0-9]*|0) echo "UNSTRACT_START_TIMEOUT must be a positive number of seconds" >&2; exit 2 ;;
esac
export DOCKER_HOST="${DOCKER_HOST:-unix://${XDG_RUNTIME_DIR:?}/podman/podman.sock}"
compose=(docker compose --project-directory "$UNSTRACT_PROJECT_DIRECTORY")
"${compose[@]}" config --quiet
services=$("${compose[@]}" config --services)

# Database-dependent application imports can fail before the worker starts.
# Gate those imports on dependency readiness, not Podman's arbitrary ID order.
dependencies=()
for service in db redis rabbitmq minio milvus-etcd milvus-minio; do
  if grep -Fxq "$service" <<< "$services"; then
    dependencies+=("$service")
  fi
done
if ((${#dependencies[@]} == 0)); then
  echo "No expected Unstract dependencies in the selected Compose project" >&2
  exit 2
fi
up=(up --detach --no-recreate --no-build --pull never --wait
    --wait-timeout "${UNSTRACT_START_TIMEOUT:-300}")
"${compose[@]}" "${up[@]}" "${dependencies[@]}"
"${compose[@]}" "${up[@]}"
