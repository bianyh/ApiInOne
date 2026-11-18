#!/usr/bin/env bash
set -u

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || exit 1
python_command=${RELAY_PYTHON:-python3}
child_pid=""

stop_relay() {
    trap - INT TERM
    if [[ -n "$child_pid" ]]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" || true
    fi
    exit 0
}
trap stop_relay INT TERM

while true; do
    "$python_command" app.py &
    child_pid=$!
    wait "$child_pid"
    status=$?
    child_pid=""
    case "$status" in
        0|130|143) exit "$status" ;;
    esac
    printf 'ApiInOne exited with status %s; restarting in 5 seconds.\n' "$status" >&2
    sleep 5
done
