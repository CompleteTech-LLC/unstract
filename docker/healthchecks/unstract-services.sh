#!/bin/sh
# Read-only readiness probes for the core Train Unstract services.
#
# The script is intentionally dependency-light: each probe uses a client that is
# already shipped in the service image. It emits only a short, stable failure
# reason so Podman health logs never contain response bodies or credentials.
set -eu

service=${1:-}
timeout_seconds=${HEALTHCHECK_TIMEOUT_SECONDS-3}
timeout_bin=${TIMEOUT_BIN:-timeout}
head_bin=${HEAD_BIN:-head}
wc_bin=${WC_BIN:-wc}
rm_bin=${RM_BIN:-rm}
mktemp_bin=${MKTEMP_BIN:-mktemp}
mkfifo_bin=${MKFIFO_BIN:-mkfifo}

case "$timeout_seconds" in
    ''|*[!0-9]*|0*)
        printf 'unstract-health: invalid timeout configuration\n' >&2
        exit 2
        ;;
    [1-9]|1[0-9]|2[0-9]|30)
        :
        ;;
    *)
        timeout_seconds=30
        ;;
esac

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

# Use an uncatchable deadline signal. The default TERM signal can be ignored by
# a client, leaving the timeout wrapper alive while wait_for_child waits for it;
# GNU and BusyBox timeout both support the portable `-s KILL` form.
# The timeout wrapper starts client commands in their own process group on the
# supported GNU and BusyBox implementations.  A healthcheck can be signalled
# while its shell is waiting for that wrapper; terminate both the group and
# the wrapper PID so a client descendant cannot keep a FIFO open or delay the
# shell's trap indefinitely.  The group form is allowed to fail for readers
# that share the shell's process group, after which the direct PID is killed.
terminate_process_group() {
    terminate_process_pid=$1
    [ -n "$terminate_process_pid" ] || return 0
    kill -TERM -"$terminate_process_pid" >/dev/null 2>&1 || :
    kill -TERM "$terminate_process_pid" >/dev/null 2>&1 || :
    kill -KILL -"$terminate_process_pid" >/dev/null 2>&1 || :
    kill -KILL "$terminate_process_pid" >/dev/null 2>&1 || :
}

# The timeout wrapper owns the client deadline, so waiting for that wrapper is
# bounded by the same deadline and, crucially, reaps it in this shell. Do not
# poll with `kill -0`: on BusyBox, a child that has exited but is still a
# zombie continues to satisfy `kill -0`, which can discard its PID without a
# wait and leak one zombie on every health invocation.
wait_for_child() {
    wait_for_child_pid=$1
    wait "$wait_for_child_pid"
}

# BusyBox wget has no max-filesize or max-redirect option. Stream through
# bounded head processes, while capturing response headers so redirects can be
# rejected even when the client follows them internally. The status file keeps
# the upstream client status visible without relying on non-POSIX pipefail.
bounded_wget_cleanup() {
    for bounded_wget_cleanup_pid in \
        "${bounded_wget_client_pid-}" \
        "${bounded_wget_body_reader_pid-}" \
        "${bounded_wget_header_reader_pid-}"; do
        if [ -n "$bounded_wget_cleanup_pid" ]; then
            terminate_process_group "$bounded_wget_cleanup_pid"
        fi
    done
    for bounded_wget_cleanup_pid in \
        "${bounded_wget_client_pid-}" \
        "${bounded_wget_body_reader_pid-}" \
        "${bounded_wget_header_reader_pid-}"; do
        if [ -n "$bounded_wget_cleanup_pid" ]; then
            wait "$bounded_wget_cleanup_pid" >/dev/null 2>&1 || :
        fi
    done
    for bounded_wget_cleanup_file in \
        "${bounded_wget_headers-}" \
        "${bounded_wget_body_file-}" \
        "${bounded_wget_status_file-}" \
        "${bounded_wget_body_fifo-}" \
        "${bounded_wget_header_fifo-}"; do
        if [ -n "$bounded_wget_cleanup_file" ]; then
            "$rm_bin" -f "$bounded_wget_cleanup_file" >/dev/null 2>&1 || :
        fi
    done
}

bounded_wget_finish() {
    trap - HUP INT TERM EXIT
    bounded_wget_cleanup
}

bounded_wget_abort() {
    trap - HUP INT TERM EXIT
    bounded_wget_cleanup
    exit 143
}

bounded_wget() {
    bounded_wget_url=$1
    bounded_wget_limit=$2
    bounded_wget_result_file=
    bounded_wget_headers=
    bounded_wget_body_file=
    bounded_wget_status_file=
    bounded_wget_body_fifo=
    bounded_wget_header_fifo=
    bounded_wget_client_pid=
    bounded_wget_body_reader_pid=
    bounded_wget_header_reader_pid=
    trap bounded_wget_abort HUP INT TERM
    trap bounded_wget_cleanup EXIT

    bounded_wget_headers=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-headers.XXXXXX" 2>/dev/null) || return 1
    bounded_wget_body_file=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body.XXXXXX" 2>/dev/null) || return 1
    bounded_wget_status_file=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-status.XXXXXX" 2>/dev/null) || return 1
    bounded_wget_body_fifo=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body-fifo.XXXXXX" 2>/dev/null) || return 1
    bounded_wget_header_fifo=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-header-fifo.XXXXXX" 2>/dev/null) || return 1
    "$rm_bin" -f "$bounded_wget_body_fifo" "$bounded_wget_header_fifo" >/dev/null 2>&1 || return 1
    "$mkfifo_bin" "$bounded_wget_body_fifo" >/dev/null 2>&1 || return 1
    "$mkfifo_bin" "$bounded_wget_header_fifo" >/dev/null 2>&1 || return 1
    "$head_bin" -c "$((bounded_wget_limit + 1))" <"$bounded_wget_body_fifo" >"$bounded_wget_body_file" &
    bounded_wget_body_reader_pid=$!
    "$head_bin" -c 16385 <"$bounded_wget_header_fifo" >"$bounded_wget_headers" &
    bounded_wget_header_reader_pid=$!
    "$timeout_bin" -s KILL "$timeout_seconds" "$wget_bin" -qS -O- -t 1 -T "$timeout_seconds" "$bounded_wget_url" \
        >"$bounded_wget_body_fifo" 2>"$bounded_wget_header_fifo" &
    bounded_wget_client_pid=$!
    if wait_for_child "$bounded_wget_client_pid"; then
        bounded_wget_status=0
    else
        bounded_wget_status=$?
    fi
    bounded_wget_client_pid=
    printf '%s\n' "$bounded_wget_status" >"$bounded_wget_status_file" || return 1
    if wait "$bounded_wget_body_reader_pid"; then
        bounded_wget_body_reader_pid=
    else
        bounded_wget_body_reader_status=$?
        bounded_wget_body_reader_pid=
        return "$bounded_wget_body_reader_status"
    fi
    if wait "$bounded_wget_header_reader_pid"; then
        bounded_wget_header_reader_pid=
    else
        bounded_wget_header_reader_status=$?
        bounded_wget_header_reader_pid=
        return "$bounded_wget_header_reader_status"
    fi
    bounded_wget_status=$("$head_bin" -c 16 "$bounded_wget_status_file" 2>/dev/null) || return 1
    case "$bounded_wget_status" in
        0) ;;
        *) return 1 ;;
    esac
    bounded_wget_header_size=$("$wc_bin" -c <"$bounded_wget_headers" 2>/dev/null) || return 1
    [ "$bounded_wget_header_size" -le 16384 ] || return 1
    grep -Eiq '(^|[[:space:]])HTTP/[0-9.]+[[:space:]]+3[0-9][0-9]([[:space:]]|$)|^.*Location:' "$bounded_wget_headers" && return 1
    bounded_wget_body_size=$("$wc_bin" -c <"$bounded_wget_body_file") || return 1
    [ "$bounded_wget_body_size" -le "$bounded_wget_limit" ] || return 1
    bounded_wget_result_file=$bounded_wget_body_file
}

bounded_curl_cleanup() {
    for bounded_curl_cleanup_pid in \
        "${bounded_curl_client_pid-}" \
        "${bounded_curl_body_reader_pid-}"; do
        if [ -n "$bounded_curl_cleanup_pid" ]; then
            terminate_process_group "$bounded_curl_cleanup_pid"
        fi
    done
    for bounded_curl_cleanup_pid in \
        "${bounded_curl_client_pid-}" \
        "${bounded_curl_body_reader_pid-}"; do
        if [ -n "$bounded_curl_cleanup_pid" ]; then
            wait "$bounded_curl_cleanup_pid" >/dev/null 2>&1 || :
        fi
    done
    for bounded_curl_cleanup_file in \
        "${bounded_curl_body_file-}" \
        "${bounded_curl_status_file-}" \
        "${bounded_curl_body_fifo-}"; do
        if [ -n "$bounded_curl_cleanup_file" ]; then
            "$rm_bin" -f "$bounded_curl_cleanup_file" >/dev/null 2>&1 || :
        fi
    done
}

bounded_curl_finish() {
    trap - HUP INT TERM EXIT
    bounded_curl_cleanup
}

bounded_curl_abort() {
    trap - HUP INT TERM EXIT
    bounded_curl_cleanup
    exit 143
}

bounded_curl() {
    bounded_curl_url=$1
    bounded_curl_limit=$2
    bounded_curl_result_file=
    bounded_curl_body_file=
    bounded_curl_status_file=
    bounded_curl_body_fifo=
    bounded_curl_client_pid=
    bounded_curl_body_reader_pid=
    trap bounded_curl_abort HUP INT TERM
    trap bounded_curl_cleanup EXIT
    bounded_curl_body_file=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body.XXXXXX" 2>/dev/null) || return 1
    bounded_curl_status_file=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-status.XXXXXX" 2>/dev/null) || return 1
    bounded_curl_body_fifo=$("$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body-fifo.XXXXXX" 2>/dev/null) || return 1
    "$rm_bin" -f "$bounded_curl_body_fifo" >/dev/null 2>&1 || return 1
    "$mkfifo_bin" "$bounded_curl_body_fifo" >/dev/null 2>&1 || return 1
    "$head_bin" -c "$((bounded_curl_limit + 1))" <"$bounded_curl_body_fifo" >"$bounded_curl_body_file" &
    bounded_curl_body_reader_pid=$!
    "$timeout_bin" -s KILL "$timeout_seconds" "$curl_bin" -fsS --location --max-redirs 0 --max-filesize "$bounded_curl_limit" \
        --max-time "$timeout_seconds" "$bounded_curl_url" >"$bounded_curl_body_fifo" &
    bounded_curl_client_pid=$!
    if wait_for_child "$bounded_curl_client_pid"; then
        bounded_curl_status=0
    else
        bounded_curl_status=$?
    fi
    bounded_curl_client_pid=
    printf '%s\n' "$bounded_curl_status" >"$bounded_curl_status_file" || return 1
    if wait "$bounded_curl_body_reader_pid"; then
        bounded_curl_body_reader_pid=
    else
        bounded_curl_body_reader_status=$?
        bounded_curl_body_reader_pid=
        return "$bounded_curl_body_reader_status"
    fi
    bounded_curl_status=$("$head_bin" -c 16 "$bounded_curl_status_file" 2>/dev/null) || return 1
    case "$bounded_curl_status" in
        0) ;;
        *) return 1 ;;
    esac
    bounded_curl_body_size=$("$wc_bin" -c <"$bounded_curl_body_file") || return 1
    [ "$bounded_curl_body_size" -le "$bounded_curl_limit" ] || return 1
    bounded_curl_result_file=$bounded_curl_body_file
}

# Native command clients can also return an unexpectedly large response before
# exiting.  Keep their stdout in a bounded FIFO capture and apply the same
# outer deadline used by HTTP clients.  This is used for the Redis and
# PostgreSQL probes, whose contracts only need a tiny scalar response.
bounded_exec_cleanup() {
    for bounded_exec_cleanup_pid in \
        "${bounded_exec_client_pid-}" \
        "${bounded_exec_reader_pid-}"; do
        if [ -n "$bounded_exec_cleanup_pid" ]; then
            terminate_process_group "$bounded_exec_cleanup_pid"
        fi
    done
    for bounded_exec_cleanup_pid in \
        "${bounded_exec_client_pid-}" \
        "${bounded_exec_reader_pid-}"; do
        if [ -n "$bounded_exec_cleanup_pid" ]; then
            wait "$bounded_exec_cleanup_pid" >/dev/null 2>&1 || :
        fi
    done
    for bounded_exec_cleanup_file in \
        "${bounded_exec_body_file-}" \
        "${bounded_exec_status_file-}" \
        "${bounded_exec_body_fifo-}"; do
        if [ -n "$bounded_exec_cleanup_file" ]; then
            "$rm_bin" -f "$bounded_exec_cleanup_file" >/dev/null 2>&1 || :
        fi
    done
}

bounded_exec_finish() {
    trap - HUP INT TERM EXIT
    bounded_exec_cleanup
}

bounded_exec_abort() {
    trap - HUP INT TERM EXIT
    bounded_exec_cleanup
    exit 143
}

bounded_exec() {
    bounded_exec_limit=$1
    shift
    bounded_exec_result_file=
    bounded_exec_body_file=
    bounded_exec_status_file=
    bounded_exec_body_fifo=
    bounded_exec_client_pid=
    bounded_exec_reader_pid=
    trap bounded_exec_abort HUP INT TERM
    trap bounded_exec_cleanup EXIT

    bounded_exec_body_file=$(
        "$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body.XXXXXX" 2>/dev/null
    ) || return 1
    bounded_exec_status_file=$(
        "$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-status.XXXXXX" 2>/dev/null
    ) || return 1
    bounded_exec_body_fifo=$(
        "$mktemp_bin" "${TMPDIR:-/tmp}/unstract-health-body-fifo.XXXXXX" 2>/dev/null
    ) || return 1
    "$rm_bin" -f "$bounded_exec_body_fifo" >/dev/null 2>&1 || return 1
    "$mkfifo_bin" "$bounded_exec_body_fifo" >/dev/null 2>&1 || return 1
    "$head_bin" -c "$((bounded_exec_limit + 1))" <"$bounded_exec_body_fifo" \
        >"$bounded_exec_body_file" &
    bounded_exec_reader_pid=$!
    "$timeout_bin" -s KILL "$timeout_seconds" "$@" >"$bounded_exec_body_fifo" 2>/dev/null &
    bounded_exec_client_pid=$!
    if wait_for_child "$bounded_exec_client_pid"; then
        bounded_exec_status=0
    else
        bounded_exec_status=$?
    fi
    bounded_exec_client_pid=
    printf '%s\n' "$bounded_exec_status" >"$bounded_exec_status_file" || return 1
    if wait "$bounded_exec_reader_pid"; then
        bounded_exec_reader_pid=
    else
        bounded_exec_reader_status=$?
        bounded_exec_reader_pid=
        return "$bounded_exec_reader_status"
    fi
    bounded_exec_status=$(
        "$head_bin" -c 16 "$bounded_exec_status_file" 2>/dev/null
    ) || return 1
    case "$bounded_exec_status" in
        0) ;;
        *) return 1 ;;
    esac
    bounded_exec_body_size=$(
        "$wc_bin" -c <"$bounded_exec_body_file"
    ) || return 1
    [ "$bounded_exec_body_size" -le "$bounded_exec_limit" ] || return 1
    bounded_exec_result_file=$bounded_exec_body_file
}

probe_weaviate() {
    bounded_wget "$weaviate_url" 65536 || fail
    body=$("$head_bin" -c 65536 "$bounded_wget_result_file") || {
        bounded_wget_finish
        fail
    }
    bounded_wget_finish
    # /v1/meta is a bounded, application-level response. It proves that the
    # Weaviate HTTP API is serving metadata, rather than only accepting TCP.
    printf '%s' "$body" | grep -Eq '"version"[[:space:]]*:[[:space:]]*"[^"[:space:]]+"' || fail
    # Also require the official readiness endpoint. It intentionally has an
    # empty body, so its HTTP status is the contract here.
    ready_url=${WEAVIATE_READY_URL:-http://127.0.0.1:8080/v1/.well-known/ready}
    bounded_wget "$ready_url" 1024 >/dev/null 2>&1 || fail
    bounded_wget_finish
}

probe_vector_db() {
    # The Qdrant image does not ship curl/wget. Its Debian base does ship Bash,
    # so use Bash's TCP client to exercise the real REST health endpoint. The
    # response is bounded and matched on both HTTP status and body semantics.
    "$timeout_bin" -s KILL "$timeout_seconds" "$qdrant_bash_bin" -ec '
        host=$1
        port=$2
        case "$host" in
            ""|*[!A-Za-z0-9_.:-]*) exit 1 ;;
        esac
        case "$port" in
            ""|*[!0-9]*) exit 1 ;;
        esac
        [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || exit 1
        exec 3<>/dev/tcp/"$host"/"$port"
        printf "GET /healthz HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3
        response=$(head -c 512 <&3)
        case "$response" in
            *"HTTP/1.1 200"*"healthz check passed"*) exit 0 ;;
            *) exit 1 ;;
        esac
    ' -- "$qdrant_host" "$qdrant_port" 2>/dev/null || fail
}

probe_redis() {
    # PING is read-only and is authenticated automatically when the image's
    # REDISCLI_AUTH/ACL environment is supplied by Compose.
    bounded_exec 16 "$redis_cli_bin" --raw ping || fail
    response=$("$head_bin" -c 16 "$bounded_exec_result_file") || {
        bounded_exec_finish
        fail
    }
    bounded_exec_finish
    [ "$response" = PONG ] || fail
}

probe_proxy() {
    bounded_wget "$proxy_url" 65536 || fail
    body=$("$head_bin" -c 65536 "$bounded_wget_result_file") || {
        bounded_wget_finish
        fail
    }
    bounded_wget_finish
    # Traefik's overview is its own control-plane readiness contract. Require
    # at least one router and service, with no reported warnings or errors.
    printf '%s' "$body" | grep -Eq '"routers":\{"total":[1-9][0-9]*,"warnings":0,"errors":0\}' || fail
    printf '%s' "$body" | grep -Eq '"services":\{"total":[1-9][0-9]*,"warnings":0,"errors":0\}' || fail
}

probe_rabbitmq() {
    "$timeout_bin" -s KILL "$timeout_seconds" "$rabbitmq_diagnostics_bin" -q check_running >/dev/null 2>&1 || fail
    "$timeout_bin" -s KILL "$timeout_seconds" "$rabbitmq_diagnostics_bin" -q check_local_alarms >/dev/null 2>&1 || fail
}

probe_minio() {
    # MinIO's unauthenticated readiness endpoint reports cluster readiness and
    # avoids a mutating S3 operation or a dependency on an mc alias file.
    bounded_curl "${MINIO_READY_URL:-http://127.0.0.1:9000/minio/health/ready}" 1024 \
        || fail
    bounded_curl_finish
}

probe_db() {
    db_user=${POSTGRES_USER:-postgres}
    db_name=${POSTGRES_DB:-postgres}
    "$timeout_bin" -s KILL "$timeout_seconds" "$pg_isready_bin" -t "$timeout_seconds" -U "$db_user" -d "$db_name" >/dev/null 2>&1 || fail
    bounded_exec 16 "$psql_bin" -XAtqc 'SELECT 1' -U "$db_user" -d "$db_name" || fail
    result=$("$head_bin" -c 16 "$bounded_exec_result_file") || {
        bounded_exec_finish
        fail
    }
    bounded_exec_finish
    [ "$result" = 1 ] || fail
}

probe_python_body() {
    url=$1
    expected=$2
    "$timeout_bin" -s KILL "$timeout_seconds" "$python_bin" -c '
import sys
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)
with opener.open(sys.argv[1], timeout=float(sys.argv[3])) as response:
    if response.status != 200 or response.read(129).decode("utf-8") != sys.argv[2]:
        raise SystemExit(1)
' "$url" "$expected" "$timeout_seconds" >/dev/null 2>&1 || fail
}

probe_x2text() {
    probe_python_body "$x2text_url" OK
}

probe_platform() {
    # platform-service opens its configured PostgreSQL connection in the
    # before-request hook, so this endpoint validates HTTP and local dependency
    # initialization together.
    probe_python_body "$platform_url" OK
}

probe_backend() {
    "$timeout_bin" -s KILL "$timeout_seconds" "$python_bin" -c '
import json
import os
import sys
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

token = os.environ.get("INTERNAL_SERVICE_API_KEY")
if not token:
    raise SystemExit(1)
request = urllib.request.Request(
    sys.argv[1], headers={"Authorization": "Bearer " + token}
)
opener = urllib.request.build_opener(NoRedirect)
with opener.open(request, timeout=float(sys.argv[2])) as response:
    if response.status != 200:
        raise SystemExit(1)
    raw = response.read(1025)
    if len(raw) > 1024:
        raise SystemExit(1)
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("status") != "healthy" or payload.get("authenticated") is not True:
        raise SystemExit(1)
' "$backend_url" "$timeout_seconds" >/dev/null 2>&1 || fail
}

probe_frontend() {
    bounded_curl "$frontend_url" 65536 || fail
    body=$("$head_bin" -c 65536 "$bounded_curl_result_file") || {
        bounded_curl_finish
        fail
    }
    bounded_curl_finish
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
