#!/usr/bin/env bash
# Start E9 Battery Monitoring on Debian / Ubuntu.
# Activates the project virtualenv, frees TCP port 8081 if it is already
# taken, then starts the app.
#
# Usage:
#   ./run_linux_debian.sh
#   ./run_linux_debian.sh 30AE7BE844CF
#   ./run_linux_debian.sh 30AE7BE844CF,AABBCCDDEEFF
#
# Optional environment:
#   E9_WEB_HOST   bind address (default 0.0.0.0 so other machines can open the UI)
#   E9_WEB_PORT   listen port  (default 8081)

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

PORT="${E9_WEB_PORT:-8081}"
export E9_WEB_HOST="${E9_WEB_HOST:-0.0.0.0}"
export E9_WEB_PORT="$PORT"

ssbin() {
  if command -v ss >/dev/null 2>&1; then
    command -v ss
  elif [ -x /usr/sbin/ss ]; then
    echo /usr/sbin/ss
  elif [ -x /bin/ss ]; then
    echo /bin/ss
  fi
}

fuserbin() {
  if command -v fuser >/dev/null 2>&1; then
    command -v fuser
  elif [ -x /usr/sbin/fuser ]; then
    echo /usr/sbin/fuser
  elif [ -x /bin/fuser ]; then
    echo /bin/fuser
  fi
}

port_in_use() {
  local ss
  ss="$(ssbin)"
  if [ -n "$ss" ]; then
    "$ss" -lnt 2>/dev/null | awk '{print $4}' | grep -Eq ":${PORT}$"
    return $?
  fi
  (echo >/dev/tcp/127.0.0.1/"$PORT") >/dev/null 2>&1
}

port_pids() {
  local ss fuser_bin pids=""
  ss="$(ssbin)"
  if [ -n "$ss" ]; then
    pids="$("$ss" -lptn 2>/dev/null | awk -v p=":${PORT}" '
      $4 ~ p"$" {
        while (match($0, /pid=[0-9]+/)) {
          print substr($0, RSTART+4, RLENGTH-4)
          $0 = substr($0, RSTART+RLENGTH)
        }
      }' | sort -u | tr '\n' ' ')"
  fi
  fuser_bin="$(fuserbin)"
  if [ -z "${pids// /}" ] && [ -n "$fuser_bin" ]; then
    pids="$("$fuser_bin" "${PORT}/tcp" 2>/dev/null | grep -Eo '[0-9]+' | sort -u | tr '\n' ' ')"
  fi
  echo "$pids" | xargs
}

try_kill() {
  local sig="$1"
  shift
  if [ "$#" -eq 0 ]; then
    return 1
  fi
  if kill "-$sig" "$@" >/dev/null 2>&1; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n kill "-$sig" "$@" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

free_port() {
  local pids fuser_bin

  if ! port_in_use; then
    echo "Port ${PORT} is free."
    return 0
  fi

  pids="$(port_pids)"
  if [ -n "$pids" ]; then
    echo "Port ${PORT} is in use by PID(s): ${pids} — stopping them."
    try_kill TERM $pids || true
    sleep 1
    pids="$(port_pids)"
    if [ -n "$pids" ] && port_in_use; then
      echo "PID(s) still holding port ${PORT}; sending SIGKILL."
      try_kill KILL $pids || true
      sleep 1
    fi
  fi

  if port_in_use; then
    fuser_bin="$(fuserbin)"
    if [ -n "$fuser_bin" ]; then
      echo "Port ${PORT} still in use; stopping listener with fuser."
      "$fuser_bin" -k -TERM "${PORT}/tcp" >/dev/null 2>&1 || \
        sudo -n "$fuser_bin" -k -TERM "${PORT}/tcp" >/dev/null 2>&1 || true
      sleep 1
      if port_in_use; then
        "$fuser_bin" -k -KILL "${PORT}/tcp" >/dev/null 2>&1 || \
          sudo -n "$fuser_bin" -k -KILL "${PORT}/tcp" >/dev/null 2>&1 || true
        sleep 1
      fi
    fi
  fi

  local i
  for i in $(seq 1 20); do
    if ! port_in_use; then
      echo "Port ${PORT} is now free."
      return 0
    fi
    sleep 0.25
  done

  echo "ERROR: port ${PORT} is still in use. Stop the process manually, then retry." >&2
  echo "  ss -lptn | grep ${PORT}" >&2
  echo "  sudo fuser -k ${PORT}/tcp" >&2
  exit 1
}

activate_venv() {
  local activate="$ROOT/venv/bin/activate"
  if [ ! -f "$activate" ] || [ ! -x "$ROOT/venv/bin/python" ]; then
    echo "ERROR: virtualenv missing at ${ROOT}/venv. Run ./install_debian.sh first." >&2
    echo "  sudo ./install_debian.sh" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$activate"
  if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "ERROR: failed to activate virtualenv at ${ROOT}/venv." >&2
    exit 1
  fi
  hash -r
  echo "Activated virtualenv: $VIRTUAL_ENV"
}

activate_venv

PYVER="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYMAJ="$(python -c 'import sys; print(sys.version_info[0])')"
PYMIN="$(python -c 'import sys; print(sys.version_info[1])')"
if [ "$PYMAJ" -lt 3 ] || [ "$PYMIN" -lt 7 ]; then
  echo "ERROR: Python ${PYVER} is too old (need 3.7+)." >&2
  echo "Run ./install_debian.sh to create a Python 3.7+ virtualenv." >&2
  exit 1
fi

free_port

echo "Starting E9 Battery Monitoring on http://${E9_WEB_HOST}:${PORT}/"
echo "Python: $(command -v python) ($PYVER)"
exec python "$ROOT/main.py" "$@"
