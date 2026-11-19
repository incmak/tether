#!/bin/sh
# Both the installed hook and the manual integration use this watchdog.
py=${2:-${TETHER_PYTHON:-python3}}
gate=${3:-"$(dirname "$0")/commit_gate.py"}
deadline=${4:-10}
[ -f "$gate" ] && command -v "$py" >/dev/null 2>&1 || exit 0
"$py" "$gate" commit-msg "$1" &
child=$!
(
    timer= cancelled=
    stop_timer() {
        kill "$timer" 2>/dev/null
        wait "$timer" 2>/dev/null
        exit 0
    }
    trap 'cancelled=1; [ -z "$timer" ] || stop_timer' TERM
    sleep "$deadline" &
    timer=$!
    [ -z "$cancelled" ] || stop_timer
    wait "$timer"
    kill -KILL "$child" 2>/dev/null
) </dev/null >/dev/null 2>&1 &
watcher=$!
wait "$child"; rc=$?
kill "$watcher" 2>/dev/null
wait "$watcher" 2>/dev/null
[ "$rc" -eq 3 ] && exit 1
exit 0
