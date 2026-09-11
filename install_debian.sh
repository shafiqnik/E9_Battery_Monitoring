#!/usr/bin/env bash
# Install E9 Battery Monitoring on Debian / Ubuntu.
# Installs Python 3, python3-venv, iproute2, and psmisc, then creates a
# local virtualenv so the app does not depend on system site-packages.
#
# Copy this whole project folder to the Linux machine, then:
#   chmod +x install_debian.sh run_linux_debian.sh
#   sudo ./install_debian.sh
#   ./run_linux_debian.sh
#
# Optional:
#   sudo ./install_debian.sh --service     # systemd service (root)
#   ./install_debian.sh --crontab          # @reboot crontab (no sudo)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${E9_WEB_PORT:-8081}"
GET_PIP_URL="${GET_PIP_URL:-https://bootstrap.pypa.io/get-pip.py}"
REAL_USER="${SUDO_USER:-$USER}"
INSTALL_SERVICE=0
INSTALL_CRONTAB=0

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

usage() {
  cat <<EOF
Usage: $0 [options]

  --service     Install and start a systemd service (system service if
                run with sudo; user service otherwise)
  --crontab     Add @reboot crontab so it starts after reboot (no sudo)
  -h, --help    Show this help

Examples:
  sudo ./install_debian.sh
  sudo ./install_debian.sh --service
  ./install_debian.sh --crontab
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
      echo "$cand"
      return 0
    fi
  done
  return 1
}

need_root() {
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  if have_cmd sudo && sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  echo "Need root (or passwordless sudo) to install packages." >&2
  echo "Re-run: sudo $0" >&2
  exit 1
}

install_os_packages() {
  if ! have_cmd apt-get; then
    echo "ERROR: apt-get not found. This installer targets Debian/Ubuntu." >&2
    exit 1
  fi
  need_root
  echo "Installing python3, python3-venv, python3-pip, psmisc, iproute2..."
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -y
  if ! $SUDO apt-get install -y python3 python3-venv python3-pip psmisc iproute2 curl; then
    echo "ERROR: apt-get install failed." >&2
    exit 1
  fi
}

run_as_real_user() {
  if [ "$(id -u)" -eq 0 ] && [ -n "${REAL_USER:-}" ] && [ "$REAL_USER" != "root" ]; then
    su -s /bin/bash "$REAL_USER" -c "$*"
  else
    bash -c "$*"
  fi
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
    echo "WARNING: missing $ROOT/credentials.txt — defaults will be used." >&2
    echo "Create it with username=, password=, broker=, port=, topic=, esn= if needed." >&2
  fi
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
ExecStart=${ROOT}/run_linux_debian.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

install_system_service() {
  local unit=/etc/systemd/system/e9-battery-monitoring.service
  echo "Installing system service as user ${REAL_USER}"
  write_unit "$unit" "User=${REAL_USER}"
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
    echo "Use:  ./install_debian.sh --crontab" >&2
    return 1
  fi
  systemctl --user enable --now e9-battery-monitoring.service
  echo "Started user service e9-battery-monitoring"
  echo "  systemctl --user status e9-battery-monitoring"
  echo
  echo "This user service runs while you are logged in."
  echo "To keep it after reboot/logout, an admin must run:"
  echo "  sudo loginctl enable-linger ${USER}"
  echo "Or add a crontab (no sudo):  $0 --crontab"
}

install_reboot_cron() {
  local marker="E9 Battery Monitoring"
  local line="@reboot cd ${ROOT} && E9_WEB_HOST=0.0.0.0 E9_WEB_PORT=${PORT} ${ROOT}/run_linux_debian.sh >> ${ROOT}/e9-service.log 2>&1"
  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v "$marker" | grep -v "${ROOT}/run_linux_debian.sh" > "$tmp" || true
  {
    echo "# ${marker}"
    echo "$line"
  } >> "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  echo "Installed crontab @reboot entry."
  echo "  crontab -l"
}

echo "=== E9 Battery Monitoring — Debian/Ubuntu install ==="
echo "Project: $ROOT"

NEED_PKGS=0
if ! pick_system_python >/dev/null; then
  NEED_PKGS=1
fi
if ! python_ok python3; then
  NEED_PKGS=1
fi
if ! have_cmd ss && [ ! -x /usr/sbin/ss ] && [ ! -x /bin/ss ]; then
  NEED_PKGS=1
fi
if ! have_cmd fuser && [ ! -x /usr/sbin/fuser ] && [ ! -x /bin/fuser ]; then
  NEED_PKGS=1
fi
# Debian python3 has no ensurepip unless python3-venv is installed.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  NEED_PKGS=1
fi
if [ "$NEED_PKGS" -eq 1 ]; then
  install_os_packages
fi

PYBIN="$(pick_system_python || true)"
if [ -z "${PYBIN:-}" ]; then
  echo "ERROR: could not find Python 3.7+ after package install." >&2
  exit 1
fi
PYBIN="$(command -v "$PYBIN")"
echo "Using interpreter: $PYBIN ($("$PYBIN" --version 2>&1))"

ensure_credentials

if [ -d "$ROOT/venv" ] && [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "Removing unusable venv (often a Windows venv copied onto Linux)..."
  rm -rf "$ROOT/venv"
fi

echo "Creating virtualenv at $ROOT/venv"
if ! run_as_real_user "cd '$ROOT' && '$PYBIN' -m venv '$ROOT/venv'"; then
  echo "Normal venv failed. Retrying after ensuring python3-venv..."
  need_root
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get install -y python3-venv
  run_as_real_user "cd '$ROOT' && '$PYBIN' -m venv '$ROOT/venv'"
fi

if [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "ERROR: venv has no python at $ROOT/venv/bin/python" >&2
  exit 1
fi

if ! "$ROOT/venv/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "Bootstrapping pip inside the venv"
  download "$GET_PIP_URL" "$ROOT/get-pip.py"
  run_as_real_user "cd '$ROOT' && '$ROOT/venv/bin/python' '$ROOT/get-pip.py'"
fi

run_as_real_user "cd '$ROOT' && '$ROOT/venv/bin/python' -m pip install --upgrade pip && '$ROOT/venv/bin/python' -m pip install -r '$ROOT/requirements.txt' && '$ROOT/venv/bin/python' -c 'import paho.mqtt.client as mqtt; print(\"paho-mqtt import ok\")'"

chmod +x "$ROOT/run_linux_debian.sh" "$ROOT/install_debian.sh"

if have_cmd ufw && $SUDO ufw status 2>/dev/null | grep -qi "Status: active"; then
  echo "Opening TCP ${PORT} in ufw..."
  $SUDO ufw allow "${PORT}/tcp" comment "E9 Battery Monitoring" || true
  echo "ufw: port ${PORT}/tcp allowed"
else
  echo "ufw not active; if you cannot open the UI remotely, allow TCP ${PORT} yourself."
fi

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
echo "Install finished. Start the dashboard with:"
echo "  $ROOT/run_linux_debian.sh"
echo "Dashboard: http://<this-host>:${PORT}"
echo "MQTT settings: $ROOT/credentials.txt"
