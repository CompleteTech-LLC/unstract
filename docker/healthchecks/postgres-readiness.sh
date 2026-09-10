#!/bin/sh
# Native PostgreSQL liveness plus an authenticated, read-only SELECT 1.
# Credentials stay in the container environment and are never printed.
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

host=${PGHOST:-127.0.0.1}
port=${PGPORT:-5432}
case "$port" in
  ''|*[!0-9]*) exit 1 ;;
esac

# Keep each native client bounded even when the healthcheck runner itself is
# misconfigured. GNU timeout starts the client in its own process group; its
# TERM/KILL sequence therefore also cleans up descendants. A bounded FIFO
# reader prevents an unhealthy client from making command substitution retain
# unbounded output in memory.
probe_timeout=3s
probe_max_output_bytes=4096
probe_tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/postgres-readiness.XXXXXX") || exit 1

runner_pid=
reader_pid=

# Stop the FIFO reader and the native client together. The reader is separate
# from the client group, while the timeout/client group may contain children
# that ignore TERM. Send TERM first, then KILL after a bounded grace period,
# and reap every process before returning the probe's failure status.
cleanup_processes() {
  active=0
  if [ -n "${reader_pid:-}" ]; then
    kill -TERM "$reader_pid" 2>/dev/null || true
    active=1
  fi
  if [ -n "${runner_pid:-}" ]; then
    kill -TERM -"$runner_pid" 2>/dev/null || true
    active=1
  fi
  if [ "$active" -eq 1 ]; then
    sleep 1
    if [ -n "${reader_pid:-}" ]; then
      kill -KILL "$reader_pid" 2>/dev/null || true
    fi
    if [ -n "${runner_pid:-}" ]; then
      kill -KILL -"$runner_pid" 2>/dev/null || true
    fi
    if [ -n "${reader_pid:-}" ]; then
      wait "$reader_pid" 2>/dev/null || true
      reader_pid=
    fi
    if [ -n "${runner_pid:-}" ]; then
      wait "$runner_pid" 2>/dev/null || true
      runner_pid=
    fi
  fi
}

# A client can exit successfully while a descendant still has the FIFO open.
# Once the timeout/client status is known, close that whole process group
# before waiting for the reader so a successful direct exit cannot leak the
# reader indefinitely.
stop_runner_group() {
  pid=$1
  [ -n "$pid" ] || return 0
  kill -TERM -"$pid" 2>/dev/null || true
  kill -KILL -"$pid" 2>/dev/null || true
}

wait_reader_bounded() {
  polls=0
  while kill -0 "$reader_pid" 2>/dev/null; do
    if [ "$polls" -ge 5 ]; then
      kill -TERM "$reader_pid" 2>/dev/null || true
      kill -KILL "$reader_pid" 2>/dev/null || true
      break
    fi
    sleep 0.05
    polls=$((polls + 1))
  done
  reader_status=0
  wait "$reader_pid" 2>/dev/null || reader_status=$?
  reader_pid=
}

cleanup_exit() {
  status=$?
  trap - EXIT
  trap ':' HUP INT TERM
  cleanup_processes
  rm -rf "$probe_tmp_dir"
  exit "$status"
}

cleanup_signal() {
  status=$1
  trap - EXIT
  trap ':' HUP INT TERM
  cleanup_processes
  rm -rf "$probe_tmp_dir"
  exit "$status"
}

trap cleanup_exit EXIT
trap 'cleanup_signal 129' HUP
trap 'cleanup_signal 130' INT
trap 'cleanup_signal 143' TERM

run_bounded() {
  output_file=$1
  shift
  fifo="$output_file.fifo"
  rm -f "$fifo"
  mkfifo "$fifo" || return 1
  timeout --signal=TERM --kill-after=1s "$probe_timeout" "$@" \
    >"$fifo" 2>/dev/null &
  runner_pid=$!
  dd if="$fifo" bs=1 count=$((probe_max_output_bytes + 1)) \
    >"$output_file" 2>/dev/null &
  reader_pid=$!
  status=0
  wait "$runner_pid" 2>/dev/null || status=$?
  stop_runner_group "$runner_pid"
  runner_pid=
  wait_reader_bounded
  rm -f "$fifo"
  [ "$status" -eq 0 ] || return 1
  [ "$reader_status" -eq 0 ] || return 1
  output_bytes=$(wc -c <"$output_file") || return 1
  case "$output_bytes" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$output_bytes" -le "$probe_max_output_bytes" ]
}

# Keep server liveness distinct from authentication/query readiness. A server
# can answer pg_isready while the application identity is rejected.
run_bounded "$probe_tmp_dir/liveness" pg_isready -q -h "$host" -p "$port" \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1

PGPASSWORD="$POSTGRES_PASSWORD" run_bounded "$probe_tmp_dir/query" psql \
  --no-psqlrc --no-password --quiet --no-align --tuples-only \
  --set=ON_ERROR_STOP=1 \
  --host="$host" --port="$port" \
  --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --command='SELECT 1' || exit 1

result=$(cat "$probe_tmp_dir/query") || exit 1

[ "$result" = 1 ]
