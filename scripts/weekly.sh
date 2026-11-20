#!/usr/bin/env bash
# One full pass: ingest, score, forecast, event test, report; then commit, timestamp and push the ledger.
# Safe to run again at any time. Exit code 3 means the ingest is still catching up: run it again.
set -uo pipefail
cd "$(dirname "$0")/.."

uv run shell-ledger weekly "$@"
code=$?
if [ "$code" -ne 0 ]; then
  exit "$code"
fi

git add ledger
if git diff --cached --quiet; then
  echo "ledger unchanged"
  exit 0
fi
new_forecasts=$(git diff --cached --name-only --diff-filter=A -- ledger/forecasts | grep '\.json$' || true)
git commit -q -m "ledger: $(date -u +%Y-%m-%dT%H:%MZ)"

# An independent timestamp for each new forecast, anchored outside GitHub.
for f in $new_forecasts; do
  if uvx --quiet --from opentimestamps-client ots stamp "$f"; then
    git add "$f.ots"
  else
    echo "timestamp failed for $f (the commit still stands)"
  fi
done
git diff --cached --quiet || git commit -q -m "ledger: timestamps"

if git remote get-url origin >/dev/null 2>&1; then
  git push -q origin HEAD || echo "push failed: the commits are local only"
else
  echo "no remote set: the commits are local only"
fi
