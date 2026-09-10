#!/bin/sh
# Bounded, body-suppressing HTTP readiness for the existing Milvus/MinIO rows.
# The URL is a fixed Compose healthcheck argument, never a credential source.
set -eu

url=${1:?health URL is required}

probe_timeout=3s
probe_max_output_bytes=4096
probe_tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/http-readiness.XXXXXX") || exit 1

runner_pid=
reader_pid=

# Stop the FIFO reader and curl together. The reader is separate from the
# timeout/curl group, which may contain descendants that ignore TERM. Send
# TERM first, then KILL after a bounded grace period, and reap every process.
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

# curl may exit successfully while a descendant still has the FIFO open.
# Close the whole timeout/curl group before waiting for the reader so a direct
# success cannot leak the reader indefinitely.
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

fifo="$probe_tmp_dir/body.fifo"
mkfifo "$fifo" || exit 1
timeout --signal=TERM --kill-after=1s "$probe_timeout" curl \
  --fail --silent --show-error --max-time 3 "$url" \
  >"$fifo" 2>/dev/null &
runner_pid=$!
dd if="$fifo" bs=1 count=$((probe_max_output_bytes + 1)) \
  >"$probe_tmp_dir/body" 2>/dev/null &
reader_pid=$!
status=0
wait "$runner_pid" 2>/dev/null || status=$?
stop_runner_group "$runner_pid"
runner_pid=
wait_reader_bounded
rm -f "$fifo"
[ "$status" -eq 0 ] || exit 1
[ "$reader_status" -eq 0 ] || exit 1
output_bytes=$(wc -c <"$probe_tmp_dir/body") || exit 1
case "$output_bytes" in
  ''|*[!0-9]*) exit 1 ;;
esac
[ "$output_bytes" -le "$probe_max_output_bytes" ] || exit 1

exit 0
