#!/bin/sh
# Read-only readiness probes for the core Train Unstract services.
#
# The script is intentionally dependency-light: each probe uses a client that is
# already shipped in the service image.  It emits only a short, stable failure
# reason so Podman health logs never contain response bodies or credentials.
set -eu

service=${1:-}
timeout_seconds=${HEALTHCHECK_TIMEOUT_SECONDS:-3}
timeout_bin=${TIMEOUT_BIN:-timeout}

fail() {
    printf 'unstract-health: %s probe failed\n' "$service" >&2
    exit 1
}

usage() {
    printf '%s\n' \
        'usage: unstract-services.sh {weaviate|vector-db|redis|proxy|rabbitmq|minio|db|x2text-service|platform-service|backend|frontend}' \
        >&2
    exit 2
}

[ -n "$service" ] || usage

# Every endpoint is overridable for contract tests and disposable local checks;
# production defaults are the service-local listeners used by compose.train.yaml.
weaviate_url=${WEAVIATE_META_URL:-http://127.0.0.1:8080/v1/meta}
qdrant_host=${QDRANT_HOST:-127.0.0.1}
qdrant_port=${QDRANT_PORT:-6333}
proxy_url=${TRAEFIK_OVERVIEW_URL:-http://127.0.0.1:8080/api/overview}
x2text_url=${X2TEXT_HEALTH_URL:-http://127.0.0.1:3004/api/v1/x2text/health}
platform_url=${PLATFORM_HEALTH_URL:-http://127.0.0.1:3001/health}
backend_url=${BACKEND_HEALTH_URL:-http://127.0.0.1:8000/internal/v1/health/}
frontend_url=${FRONTEND_INDEX_URL:-http://127.0.0.1:8080/}

wget_bin=${WGET_BIN:-wget}
curl_bin=${CURL_BIN:-curl}
python_bin=${PYTHON_BIN:-.venv/bin/python}
qdrant_bash_bin=${QDRANT_BASH_BIN:-bash}
redis_cli_bin=${REDIS_CLI_BIN:-redis-cli}
rabbitmq_diagnostics_bin=${RABBITMQ_DIAGNOSTICS_BIN:-rabbitmq-diagnostics}
pg_isready_bin=${PG_ISREADY_BIN:-pg_isready}
psql_bin=${PSQL_BIN:-psql}

probe_weaviate() {
    body=$("$wget_bin" -qO- --timeout="$timeout_seconds" "$weaviate_url" 2>/dev/null) || fail
    # /v1/meta is a bounded, application-level response.  It proves that the
    # Weaviate HTTP API is serving its metadata, rather than only accepting TCP.
    printf '%s' "$body" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"[^"[:space:]]+"' || fail
    # Also require the official readiness endpoint.  It intentionally has an
    # empty body, so its HTTP status is the contract here.
    ready_url=${WEAVIATE_READY_URL:-http://127.0.0.1:8080/v1/.well-known/ready}
    "$wget_bin" -qO /dev/null --timeout="$timeout_seconds" "$ready_url" 2>/dev/null || fail
}

probe_vector_db() {
    # The Qdrant image does not ship curl/wget.  Its Debian base does ship Bash,
    # so use Bash's TCP client to exercise the real REST health endpoint.  The
    # response is bounded and matched on both HTTP status and body semantics.
    "$timeout_bin" "$timeout_seconds" "$qdrant_bash_bin" -ec '
        exec 3<>/dev/tcp/'"$qdrant_host"'/'"$qdrant_port"'
        printf "GET /healthz HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3
        response=$(head -c 512 <&3)
        case "$response" in
            *"HTTP/1.1 200"*"healthz check passed"*) exit 0 ;;
            *) exit 1 ;;
        esac
    ' 2>/dev/null || fail
}

probe_redis() {
    # PING is read-only and is authenticated automatically when the image's
    # REDISCLI_AUTH/ACL environment is supplied by Compose.
    response=$("$timeout_bin" "$timeout_seconds" "$redis_cli_bin" --raw ping 2>/dev/null) || fail
    [ "$response" = PONG ] || fail
}

probe_proxy() {
    body=$("$wget_bin" -qO- --timeout="$timeout_seconds" "$proxy_url" 2>/dev/null) || fail
    # Traefik's overview is its own control-plane readiness contract.  Require
    # at least one router and service, with no reported warnings or errors.
    printf '%s' "$body" | grep -Eq '"routers":\{"total":[1-9][0-9]*,"warnings":0,"errors":0\}' || fail
    printf '%s' "$body" | grep -Eq '"services":\{"total":[1-9][0-9]*,"warnings":0,"errors":0\}' || fail
}

probe_rabbitmq() {
    "$timeout_bin" "$timeout_seconds" "$rabbitmq_diagnostics_bin" -q check_running >/dev/null 2>&1 || fail
    "$timeout_bin" "$timeout_seconds" "$rabbitmq_diagnostics_bin" -q check_local_alarms >/dev/null 2>&1 || fail
}

probe_minio() {
    # MinIO's unauthenticated readiness endpoint reports cluster readiness and
    # avoids a mutating S3 operation or a dependency on an mc alias file.
    "$curl_bin" -fsS --max-time "$timeout_seconds" \
        "${MINIO_READY_URL:-http://127.0.0.1:9000/minio/health/ready}" \
        >/dev/null 2>&1 || fail
}

probe_db() {
    db_user=${POSTGRES_USER:-postgres}
    db_name=${POSTGRES_DB:-postgres}
    "$timeout_bin" "$timeout_seconds" "$pg_isready_bin" -t "$timeout_seconds" -U "$db_user" -d "$db_name" >/dev/null 2>&1 || fail
    result=$("$timeout_bin" "$timeout_seconds" "$psql_bin" -XAtqc 'SELECT 1' -U "$db_user" -d "$db_name" 2>/dev/null) || fail
    [ "$result" = 1 ] || fail
}

probe_python_body() {
    url=$1
    expected=$2
    "$python_bin" -c '
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[3])) as response:
    if response.status != 200 or response.read(128).decode("utf-8") != sys.argv[2]:
        raise SystemExit(1)
' "$url" "$expected" "$timeout_seconds" >/dev/null 2>&1 || fail
}

probe_x2text() {
    probe_python_body "$x2text_url" OK
}

probe_platform() {
    # platform-service opens its configured PostgreSQL connection in the
    # before-request hook, so this endpoint validates both HTTP and that local
    # dependency initialization completed.
    probe_python_body "$platform_url" OK
}

probe_backend() {
    "$python_bin" -c '
import json
import os
import sys
import urllib.request

token = os.environ.get("INTERNAL_SERVICE_API_KEY")
if not token:
    raise SystemExit(1)
request = urllib.request.Request(
    sys.argv[1], headers={"Authorization": "Bearer " + token}
)
with urllib.request.urlopen(request, timeout=float(sys.argv[2])) as response:
    if response.status != 200:
        raise SystemExit(1)
    payload = json.loads(response.read(1024).decode("utf-8"))
    if payload.get("status") != "healthy" or payload.get("authenticated") is not True:
        raise SystemExit(1)
' "$backend_url" "$timeout_seconds" >/dev/null 2>&1 || fail
}

probe_frontend() {
    body=$("$curl_bin" -fsS --max-time "$timeout_seconds" "$frontend_url" 2>/dev/null) || fail
    case "$body" in
        *'<title>Unstract</title>'*) : ;;
        *) fail ;;
    esac
}

case "$service" in
    weaviate) probe_weaviate ;;
    vector-db) probe_vector_db ;;
    redis) probe_redis ;;
    proxy) probe_proxy ;;
    rabbitmq) probe_rabbitmq ;;
    minio) probe_minio ;;
    db) probe_db ;;
    x2text-service) probe_x2text ;;
    platform-service) probe_platform ;;
    backend) probe_backend ;;
    frontend) probe_frontend ;;
    *) usage ;;
esac

exit 0
