#!/usr/bin/env bash
# Set up E9 Battery Monitoring on Linux without requiring sudo, then
# optionally install it as a service.
#
# From the project folder:
#   chmod +x install_linux.sh run_linux.sh
#   ./install_linux.sh              # venv + pip packages
#   ./install_linux.sh --service    # same, then start at login (user systemd)
#   sudo ./install_linux.sh --service   # boot service for all users (needs root)
#
# After --service:
#   systemctl --user status e9-battery-monitoring
#   systemctl --user restart e9-battery-monitoring
#   journalctl --user -u e9-battery-monitoring -f

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${E9_WEB_PORT:-8081}"
GET_PIP_URL="${GET_PIP_URL:-https://bootstrap.pypa.io/get-pip.py}"
INSTALL_SERVICE=0
INSTALL_CRONTAB=0

usage() {
  cat <<EOF
Usage: $0 [options]

  --service     Install and start a systemd service (user service without
                sudo; system service if you run this with sudo)
  --crontab     Also add @reboot crontab so it starts after reboot without
                lingering (no sudo required)
  -h, --help    Show this help

Examples:
  ./install_linux.sh
  ./install_linux.sh --service
  ./install_linux.sh --service --crontab
  sudo ./install_linux.sh --service
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --service) INSTALL_SERVICE=1 ;;
    --crontab) INSTALL_CRONTAB=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

have_cmd() { command -v "$1" >/dev/null 2>&1; }

python_ok() {
  local bin="$1"
  have_cmd "$bin" || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)'
}

pick_system_python() {
  local cand
  for cand in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if python_ok "$cand"; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

download() {
  local url="$1" dest="$2"
  if have_cmd curl; then
    curl -fsSL "$url" -o "$dest"
  elif have_cmd wget; then
    wget -q "$url" -O "$dest"
  else
    echo "ERROR: need curl or wget to download pip." >&2
    exit 1
  fi
}

ensure_credentials() {
  if [ ! -f "$ROOT/credentials.txt" ]; then
    echo "ERROR: missing $ROOT/credentials.txt" >&2
    echo "Create it with:" >&2
    echo "  username=your_mqtt_user" >&2
    echo "  password=your_mqtt_password" >&2
    exit 1
  fi
}

create_venv() {
  local pybin="$1"
  local vpy="$ROOT/venv/bin/python"

  if [ -x "$vpy" ] && [ -f "$ROOT/venv/bin/activate" ]; then
    echo "Existing venv: $ROOT/venv"
    return 0
  fi

  if [ -d "$ROOT/venv" ]; then
    echo "Removing incomplete venv at $ROOT/venv"
    rm -rf "$ROOT/venv"
  fi

  echo "Creating virtualenv with $pybin"
  if "$pybin" -m venv "$ROOT/venv" 2>/tmp/e9-venv.err; then
    :
  else
    echo "Normal venv failed (often missing ensurepip). Retrying --without-pip..."
    if ! "$pybin" -m venv --without-pip "$ROOT/venv"; then
      echo "ERROR: python3 -m venv failed." >&2
      echo "Ask an admin to install python3-venv (Debian/Ubuntu) or python3-virtualenv." >&2
      cat /tmp/e9-venv.err >&2 || true
      exit 1
    fi
  fi

  if [ ! -x "$ROOT/venv/bin/python" ]; then
    echo "ERROR: venv has no python at $ROOT/venv/bin/python" >&2
    exit 1
  fi
}

ensure_pip() {
  local vpy="$ROOT/venv/bin/python"
  if "$vpy" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  echo "Bootstrapping pip inside the venv (does not change system Python)"
  download "$GET_PIP_URL" "$ROOT/get-pip.py"
  "$vpy" "$ROOT/get-pip.py"
}

install_python_packages() {
  local vpy="$ROOT/venv/bin/python"
  echo "Installing requirements into venv"
  "$vpy" -m pip install --upgrade pip
  "$vpy" -m pip install -r "$ROOT/requirements.txt"
  "$vpy" -c 'import paho.mqtt.client as mqtt; print("paho-mqtt import ok")'
}

write_unit() {
  local dest="$1"
  local user_line="$2"
  mkdir -p "$(dirname "$dest")"
  cat > "$dest" <<EOF
[Unit]
Description=E9 Battery Monitoring dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
${user_line}
WorkingDirectory=${ROOT}
Environment=E9_WEB_HOST=0.0.0.0
Environment=E9_WEB_PORT=${PORT}
ExecStart=${ROOT}/run_linux.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

install_system_service() {
  local unit=/etc/systemd/system/e9-battery-monitoring.service
  local run_user="${SUDO_USER:-$USER}"
  echo "Installing system service as user ${run_user}"
  write_unit "$unit" "User=${run_user}"
  # System units boot via multi-user.target
  sed -i 's|WantedBy=default.target|WantedBy=multi-user.target|' "$unit"
  systemctl daemon-reload
  systemctl enable --now e9-battery-monitoring.service
  echo "Started: e9-battery-monitoring.service"
  echo "  sudo systemctl status e9-battery-monitoring"
  echo "  sudo journalctl -u e9-battery-monitoring -f"
}

install_user_service() {
  local unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/e9-battery-monitoring.service"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  echo "Installing user service (no sudo): $unit"
  write_unit "$unit" ""
  if ! systemctl --user daemon-reload; then
    echo "WARNING: systemctl --user failed. Is systemd user session available?" >&2
    echo "Use:  ./install_linux.sh --crontab" >&2
    return 1
  fi
  systemctl --user enable --now e9-battery-monitoring.service
  echo "Started user service e9-battery-monitoring"
  echo "  systemctl --user status e9-battery-monitoring"
  echo "  systemctl --user restart e9-battery-monitoring"
  echo "  journalctl --user -u e9-battery-monitoring -f"
  echo
  echo "This user service runs while you are logged in."
  echo "To keep it running after reboot/logout, an admin must run:"
  echo "  sudo loginctl enable-linger ${USER}"
  echo "Or add a crontab (no sudo):  $0 --crontab"
}

install_reboot_cron() {
  local marker="E9 Battery Monitoring"
  local line="@reboot cd ${ROOT} && E9_WEB_HOST=0.0.0.0 E9_WEB_PORT=${PORT} ${ROOT}/run_linux.sh >> ${ROOT}/e9-service.log 2>&1"
  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v "$marker" | grep -v "${ROOT}/run_linux.sh" > "$tmp" || true
  {
    echo "# ${marker}"
    echo "$line"
  } >> "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  echo "Installed crontab @reboot entry."
  echo "  crontab -l"
}

echo "=== E9 Battery Monitoring — Linux setup ==="
echo "Project: $ROOT"

PYBIN="$(pick_system_python || true)"
if [ -z "${PYBIN:-}" ]; then
  echo "ERROR: need Python 3.7+ (python3). This machine has none on PATH." >&2
  exit 1
fi
echo "Using interpreter: $PYBIN ($("$PYBIN" --version 2>&1))"

ensure_credentials
chmod +x "$ROOT/run_linux.sh" "$ROOT/install_linux.sh" "$ROOT/install_centos8.sh" 2>/dev/null || true
create_venv "$PYBIN"
ensure_pip
install_python_packages

echo
echo "Venv ready. Foreground start:"
echo "  $ROOT/run_linux.sh"

if [ "$INSTALL_SERVICE" -eq 1 ]; then
  echo
  if [ "$(id -u)" -eq 0 ]; then
    install_system_service
  else
    install_user_service || true
  fi
fi

if [ "$INSTALL_CRONTAB" -eq 1 ]; then
  echo
  install_reboot_cron
fi

echo
echo "Dashboard: http://<this-host>:${PORT}"
echo "MQTT login: $ROOT/credentials.txt"
