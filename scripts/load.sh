#!/usr/bin/env bash
# Generates traffic against the API so the dashboards have something to show.
# usage: ./scripts/load.sh [seconds]
set -u
BASE="${BASE:-http://localhost:8000}"
DURATION="${1:-120}"
END=$(( $(date +%s) + DURATION ))

echo "hammering $BASE for ${DURATION}s ..."
while [ "$(date +%s)" -lt "$END" ]; do
  id=$(curl -s -X POST "$BASE/tasks" -H 'content-type: application/json' \
        -d "{\"title\":\"task $RANDOM\"}" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
  curl -s "$BASE/tasks" >/dev/null
  curl -s "$BASE/work" >/dev/null

  # complete roughly every other task, so the open-task gauge moves around
  if [ $((RANDOM % 2)) -eq 0 ] && [ -n "${id:-}" ]; then
    curl -s -X POST "$BASE/tasks/$id/complete" >/dev/null
  fi

  # some failures, so the error panels and error traces are not empty
  if [ $((RANDOM % 5)) -eq 0 ]; then
    curl -s "$BASE/boom" >/dev/null
    curl -s -X POST "$BASE/tasks/999999/complete" >/dev/null
  fi
  sleep 0.5
done
echo "done"
