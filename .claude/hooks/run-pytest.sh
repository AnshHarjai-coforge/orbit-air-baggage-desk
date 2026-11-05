#!/bin/sh
# Stop hook: runs pytest when Claude tries to finish.
# If tests fail, exit 2 sends Claude back to work, at most MAX_RETRIES times in a row.
# After that it lets Claude stop so a human can look at the failure.
MAX_RETRIES=1

input=$(cat)
session=$(printf '%s' "$input" | sed -n 's/.*"session_id" *: *"\([^"]*\)".*/\1/p')
counter="${TMPDIR:-/tmp}/orbit-stop-hook-${session:-default}"

# A stop that was not caused by this hook starts a fresh retry count.
case "$input" in
  *'"stop_hook_active":true'* | *'"stop_hook_active": true'*) ;;
  *) rm -f "$counter" ;;
esac

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Red stage: the last commit is a test(...) commit and nothing has changed since.
# Failing tests are expected here, so let Claude stop for review.
# As soon as feature code is written (the working tree is dirty), the hook enforces again.
last_subject=$(git log -1 --format=%s 2>/dev/null)
case "$last_subject" in
  test\(*)
    if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
      echo "Red stage (last commit is '$last_subject'): failing tests are expected." >&2
      exit 0
    fi
    ;;
esac

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

"$PY" -m pytest -q >&2
status=$?

# 0 = all passed, 5 = no tests collected yet: let Claude stop.
if [ "$status" -eq 0 ] || [ "$status" -eq 5 ]; then
  rm -f "$counter"
  exit 0
fi

count=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$counter"

if [ "$count" -gt "$MAX_RETRIES" ]; then
  echo "pytest still failing after $MAX_RETRIES retry; stopping so the user can review." >&2
  rm -f "$counter"
  exit 0
fi

echo "pytest failed (retry $count of $MAX_RETRIES): fix the code, not the tests." >&2
exit 2
