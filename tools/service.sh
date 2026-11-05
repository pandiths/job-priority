#!/bin/bash
# Run Job Radar as a macOS LaunchAgent: starts at login, restarts if it crashes.
#   tools/service.sh install    write the LaunchAgent and start it
#   tools/service.sh deploy     pull main, run the tests, restart only if they pass
#   tools/service.sh status     is it running, and is the job-search Chrome up
#   tools/service.sh logs       follow the server log
#   tools/service.sh uninstall  stop it and remove the LaunchAgent
set -euo pipefail

LABEL=com.jobradar.server
D="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$D/data/logs"
PY=/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python
NODE_BIN="$HOME/.local/node/bin"
PORT=8765
TARGET="gui/$(id -u)"

loaded() { launchctl print "$TARGET/$LABEL" >/dev/null 2>&1; }

up() {
  for _ in $(seq 1 20); do
    curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && return 0
    sleep 0.5
  done
  return 1
}

chrome() {
  if curl -fs -o /dev/null "http://127.0.0.1:9333/json/version"; then
    echo "Job-search Chrome: running on 9333"
  else
    echo "Job-search Chrome: NOT running on 9333 (Fill and Go to tab need it)"
  fi
}

run_tests() {
  echo "Running tests..."
  (cd "$D" && "$PY" -m unittest discover -s tests -q) || return 1
  (cd "$D" && PATH="$NODE_BIN:$PATH" node --test apply/test/*.test.mjs >/dev/null) || { echo "Node tests failed"; return 1; }
  echo "Tests passed"
}

install() {
  mkdir -p "$LOGS" "$(dirname "$PLIST")"
  if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! loaded; then
    echo "Something is already listening on $PORT. Stop it first."
    exit 1
  fi
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$D/radar.py</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$D</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$NODE_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGS/server.log</string>
  <key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict>
</plist>
EOF
  loaded && launchctl bootout "$TARGET/$LABEL" || true
  launchctl bootstrap "$TARGET" "$PLIST"
  if up; then echo "Job Radar is running at http://localhost:$PORT"; else echo "Didn't come up; see $LOGS/server.log"; exit 1; fi
  chrome
}

deploy() {
  cd "$D"
  if [ "$(git branch --show-current)" = main ]; then
    git pull --ff-only
  else
    echo "On branch $(git branch --show-current), not pulling"
  fi
  run_tests || { echo "Tests failed: the running server was left as it was"; exit 1; }
  # Trim the log so it never grows past ~5 MB
  if [ -f "$LOGS/server.log" ] && [ "$(stat -f%z "$LOGS/server.log")" -gt 5000000 ]; then
    tail -c 1000000 "$LOGS/server.log" >"$LOGS/server.log.tmp" && mv "$LOGS/server.log.tmp" "$LOGS/server.log"
  fi
  loaded || { echo "Not installed; run: tools/service.sh install"; exit 1; }
  launchctl kickstart -k "$TARGET/$LABEL"
  if up; then echo "Restarted: http://localhost:$PORT"; else echo "Didn't come back up; see $LOGS/server.log"; exit 1; fi
  chrome
}

status() {
  if loaded; then
    launchctl print "$TARGET/$LABEL" | grep -E "^\s*(state|pid|last exit code) =" || true
  else
    echo "Not installed"
  fi
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && echo "Server: answering on $PORT" || echo "Server: not answering on $PORT"
  chrome
}

uninstall() {
  loaded && launchctl bootout "$TARGET/$LABEL" || true
  rm -f "$PLIST"
  echo "Removed. Start it by hand with: python3 radar.py serve"
}

case "${1:-status}" in
  install) install ;;
  deploy) deploy ;;
  status) status ;;
  logs) tail -f "$LOGS/server.log" ;;
  uninstall) uninstall ;;
  *) echo "usage: $0 install|deploy|status|logs|uninstall"; exit 2 ;;
esac
