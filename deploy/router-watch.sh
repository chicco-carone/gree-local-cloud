#!/bin/sh
# Foreground worker for OpenRC; reload settings and reapply rules every 30 seconds.
set -eu
sleep_pid=
stop() {
    if [ -n "$sleep_pid" ]; then
        kill "$sleep_pid" 2>/dev/null || true
        wait "$sleep_pid" 2>/dev/null || true
    fi
    exit 0
}
trap stop INT TERM
while :; do
    if ! (
        set -a
        . /data/gree-local/routing.env
        set +a
        /bin/sh /data/gree-local/router-rules.sh
    ); then
        printf '%s\n' 'Gree routing update failed; retrying in 30 seconds.' >&2
    fi
    sleep 30 &
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
