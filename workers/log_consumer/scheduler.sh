#!/usr/bin/env bash
set -uo pipefail
# Note: pipefail without -e — one task's failure must not abort the loop.

# Task 1: log history consumer (existing).
LOG_HISTORY_INTERVAL="${LOG_HISTORY_CONSUMER_INTERVAL:-5}"
DEFAULT_LOG_HISTORY_CMD="/app/.venv/bin/python /app/log_consumer/process_log_history.py"
LOG_HISTORY_CMD="${TASK_TRIGGER_COMMAND:-$DEFAULT_LOG_HISTORY_CMD}"

# Task 2: notification buffer flush (clubbed dispatch).
# Polls on its OWN cadence (NOTIFICATION_BUFFER_POLL_INTERVAL), decoupled from
# the log-history consumer so one env knob doesn't silently govern both tasks.
# The endpoint short-circuits on an empty PENDING set, so frequent polling is
# cheap; the real dispatch cadence is gated by NOTIFICATION_CLUB_INTERVAL on the
# backend (rows precompute flush_after at enqueue time), so this only bounds how
# soon a due group is picked up.
NOTIFICATION_BUFFER_INTERVAL="${NOTIFICATION_BUFFER_POLL_INTERVAL:-10}"
DEFAULT_BUFFER_FLUSH_CMD="/app/.venv/bin/python /app/log_consumer/process_notification_buffer.py"
BUFFER_FLUSH_CMD="${NOTIFICATION_BUFFER_TASK_COMMAND:-$DEFAULT_BUFFER_FLUSH_CMD}"

# Optional local readiness endpoint. The probe reads this state file; it never
# invokes either task. A missing port preserves the historical process-only mode
# for deployments that have not yet wired a Compose healthcheck.
HEALTH_STATE_FILE="${LOG_HISTORY_SCHEDULER_HEALTH_STATE:-/tmp/log-history-scheduler-health.json}"
HEALTH_PID=""
LAST_LOG_SUCCESS=""
LAST_BUFFER_SUCCESS=""
LAST_LOG_FAILURE=""
LAST_BUFFER_FAILURE=""

# Loop wakes at the finer of the two cadences (min, floored at 1s); each task
# fires independently once its own interval has elapsed.
if [[ "${LOG_HISTORY_INTERVAL}" -lt "${NOTIFICATION_BUFFER_INTERVAL}" ]]; then
    BASE_INTERVAL="${LOG_HISTORY_INTERVAL}"
else
    BASE_INTERVAL="${NOTIFICATION_BUFFER_INTERVAL}"
fi
[[ "${BASE_INTERVAL}" -lt 1 ]] && BASE_INTERVAL=1

echo "=========================================="
echo "Log Consumer Scheduler Starting"
echo "=========================================="
echo "Log history interval: ${LOG_HISTORY_INTERVAL}s  |  Buffer flush interval: ${NOTIFICATION_BUFFER_INTERVAL}s"
echo "Task 1 (log history): ${LOG_HISTORY_CMD}"
echo "Task 2 (notification buffer flush): ${BUFFER_FLUSH_CMD}"
echo "=========================================="

write_health_state() {
    local temp_state="${HEALTH_STATE_FILE}.$$"
    if ! printf '{"parent_pid":%s,"last_log_success":%s,"last_buffer_success":%s,"last_log_failure":%s,"last_buffer_failure":%s}\n' \
        "$$" \
        "${LAST_LOG_SUCCESS:-null}" \
        "${LAST_BUFFER_SUCCESS:-null}" \
        "${LAST_LOG_FAILURE:-null}" \
        "${LAST_BUFFER_FAILURE:-null}" >"${temp_state}"; then
        echo "Warning: scheduler health state could not be written" >&2
        return 0
    fi
    if ! mv -f -- "${temp_state}" "${HEALTH_STATE_FILE}"; then
        echo "Warning: scheduler health state could not be published" >&2
    fi
}

start_health_probe() {
    if [[ -z "${LOG_HISTORY_SCHEDULER_HEALTH_PORT:-}" ]]; then
        return 0
    fi
    export LOG_HISTORY_SCHEDULER_HEALTH_PARENT_PID="$$"
    export LOG_HISTORY_SCHEDULER_HEALTH_STATE="${HEALTH_STATE_FILE}"
    write_health_state
    /app/.venv/bin/python /app/log_consumer/scheduler_health.py &
    HEALTH_PID="$!"
    echo "Scheduler health endpoint starting on :${LOG_HISTORY_SCHEDULER_HEALTH_PORT}/health"
}

cleanup() {
    echo ""
    echo "=========================================="
    echo "Scheduler received shutdown signal"
    echo "Exiting gracefully..."
    echo "=========================================="
    if [[ -n "${HEALTH_PID}" ]]; then
        kill "${HEALTH_PID}" 2>/dev/null || true
    fi
    return 0
}

# The trap exits after cleanup runs; cleanup itself returns so the function
# has an explicit terminal return (no unreachable code after exit).
trap 'cleanup; exit 0' SIGTERM SIGINT
start_health_probe

run_task() {
    # $1 = display name, $2 = command, $3 = run number. Returns the command's
    # exit code but never propagates failure — the caller logs it and moves on.
    local task_name="$1"
    local cmd="$2"
    local run_num="$3"
    local exit_code=0
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Run #${run_num}] Triggering ${task_name}..."
    if eval "${cmd}" 2>&1; then
        case "${task_name}" in
            process_log_history) LAST_LOG_SUCCESS="$(date '+%s')" ;;
            process_notification_buffer) LAST_BUFFER_SUCCESS="$(date '+%s')" ;;
        esac
        write_health_state
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Run #${run_num}] ✓ ${task_name} OK"
    else
        exit_code=$?
        case "${task_name}" in
            process_log_history) LAST_LOG_FAILURE="$(date '+%s')" ;;
            process_notification_buffer) LAST_BUFFER_FAILURE="$(date '+%s')" ;;
        esac
        write_health_state
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [Run #${run_num}] ✗ ${task_name} failed with exit code ${exit_code}"
    fi
    return "${exit_code}"
}

run_count=0
# Seed both at 0 so each task fires on the first loop iteration (now ≫ interval).
last_log_run=0
last_buffer_run=0

while true; do
    now=$(date '+%s')

    if [[ $((now - last_log_run)) -ge "${LOG_HISTORY_INTERVAL}" ]]; then
        run_count=$((run_count + 1))
        run_task "process_log_history" "${LOG_HISTORY_CMD}" "${run_count}"
        last_log_run="${now}"
    fi

    if [[ $((now - last_buffer_run)) -ge "${NOTIFICATION_BUFFER_INTERVAL}" ]]; then
        run_count=$((run_count + 1))
        run_task "process_notification_buffer" "${BUFFER_FLUSH_CMD}" "${run_count}"
        last_buffer_run="${now}"
    fi

    # Background sleep + wait so a SIGTERM/SIGINT interrupts promptly (the trap
    # fires, cleanup runs, the script exits) instead of blocking the full tick.
    sleep "${BASE_INTERVAL}" &
    wait $!
done
