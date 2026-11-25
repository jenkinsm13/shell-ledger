#!/usr/bin/env bash
# The weekly pass for sandboxed runners: the Claude desktop workspace, where the project folder
# does not allow deletes and each command is limited to three minutes.
# Run it again and again until it prints DONE. Exit code 3 means "more to do, run again".
set -uo pipefail
P="$(cd "$(dirname "$0")/.." && pwd)"
cd "$P"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/shell-ledger}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.uvcache}"

uv run -q shell-ledger weekly --max-seconds "${1:-100}"
code=$?
if [ "$code" -eq 3 ]; then echo "MORE: run again"; exit 3; fi
if [ "$code" -ne 0 ]; then echo "STOPPED with code $code"; exit "$code"; fi

# Git needs to delete its own lock files, so it works on a copy of .git kept outside the folder.
G="$HOME/shell-ledger-gitdir"
rm -rf "$G" && cp -a "$P/.git" "$G"
g() { git --git-dir="$G" --work-tree="$P" "$@"; }
export GIT_TERMINAL_PROMPT=0
# The whole history in one file under data/, so a machine that holds the GitHub login can publish it.
bundle() {
  g bundle create -q "$HOME/shell-ledger.bundle" main && cp "$HOME/shell-ledger.bundle" "$P/data/shell-ledger.bundle" \
    && echo "bundle: data/shell-ledger.bundle at $(g rev-parse --short main)"
}
g add ledger
if g diff --cached --quiet; then bundle; echo "DONE: ledger unchanged"; exit 0; fi
new=$(g diff --cached --name-only --diff-filter=A -- ledger/forecasts ledger/paper | grep '\.json$' || true)
g commit -q -m "ledger: $(date -u +%Y-%m-%dT%H:%MZ)" ${LEDGER_COMMIT_TRAILER:+-m "$LEDGER_COMMIT_TRAILER"}
for f in $new; do
  if uvx --quiet --from opentimestamps-client ots stamp "$f"; then g add "$f.ots"; else echo "timestamp failed for $f"; fi
done
g diff --cached --quiet || g commit -q -m "ledger: timestamps" ${LEDGER_COMMIT_TRAILER:+-m "$LEDGER_COMMIT_TRAILER"}
if g remote get-url origin >/dev/null 2>&1; then
  g push -q origin HEAD 2>/dev/null || echo "not pushed from here (no GitHub login in this sandbox): publish from the bundle"
else
  echo "no remote set: the commits are local only"
fi

# Copy the new history back into the folder without deleting anything.
cp -an "$G/objects/." "$P/.git/objects/" 2>/dev/null
for f in index HEAD COMMIT_EDITMSG; do [ -f "$G/$f" ] && cp "$G/$f" "$P/.git/$f"; done
(cd "$G" && find refs logs -type f 2>/dev/null) | while read -r f; do
  mkdir -p "$P/.git/$(dirname "$f")"; cp "$G/$f" "$P/.git/$f"
done
bundle
echo "DONE: ledger committed at $(g rev-parse --short HEAD)"
