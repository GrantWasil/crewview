#!/bin/zsh
# Start (or reuse) the read-only Crewview dashboard and open it in the browser.
# dashboard.py reuses a running instance only if it identifies as this dashboard
# for the same state directory; it never stops other processes. Ctrl+C stops it.
here="${0:A:h}"
CREWVIEW_PYTHON=""  # install.py records the Python 3.11+ interpreter it ran with here.
# An installed copy views the same state/ its MCP server writes; a later --state argument wins.
state=()
[[ -f "$here/.crewview-install.json" ]] && state=(--state "$here/state")
for python in "$CREWVIEW_PYTHON" python3.13 python3.12 python3.11 python3; do
  [[ -n "$python" ]] || continue
  python="$(command -v -- "$python" 2>/dev/null)" || continue
  if "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$python" "$here/dashboard.py" --open "${state[@]}" "$@"
  fi
done
echo "Python 3.11 or newer was not found; install it, then run: python3 \"$here/dashboard.py\" --open" >&2
exit 1
