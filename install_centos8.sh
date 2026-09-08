#!/usr/bin/env bash
# Install E9 Battery Monitoring on CentOS 8 / CentOS 8 Stream.
# Creates a local virtualenv so the app does not depend on system Python 3.6.
#
# Copy this whole project folder to the Linux machine, then:
#   chmod +x install_centos8.sh run_linux.sh
#   sudo ./install_centos8.sh
#   ./run_linux.sh
#
# Optional boot service:
#   sudo INSTALL_SERVICE=1 ./install_centos8.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PORT="${E9_WEB_PORT:-8081}"
REAL_USER="${SUDO_USER:-$USER}"
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

have_cmd() { command -v "$1" >/dev/null 2>&1; }

python_ok() {
  local bin="$1"
  have_cmd "$bin" || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)'
}

pick_system_python() {
  local cand
  for cand in python3.11 python3.9 python3.8 python3; do
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
  if ! have_cmd dnf; then
    echo "ERROR: dnf not found. This installer targets CentOS 8." >&2
    exit 1
  fi
  need_root
  echo "Installing Python 3.8, psmisc (fuser), and iproute (ss)..."
  if ! $SUDO dnf install -y python38 python38-pip psmisc iproute; then
    cat >&2 <<'EOF'
dnf install failed.

CentOS 8 reached end-of-life; mirrors often 404. Point yum/dnf at the vault, then retry:

  sudo sed -i 's|^mirrorlist=|#mirrorlist=|g' /etc/yum.repos.d/CentOS-*.repo
  sudo sed -i 's|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*.repo
  sudo dnf clean all
  sudo ./install_centos8.sh

On CentOS 8 Stream, keep the stream repos; do not switch them to vault.centos.org.
EOF
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

echo "=== E9 Battery Monitoring — CentOS 8 install ==="
echo "Project: $ROOT"

NEED_PKGS=0
if ! pick_system_python >/dev/null; then
  NEED_PKGS=1
fi
if ! have_cmd ss && [ ! -x /usr/sbin/ss ]; then
  NEED_PKGS=1
fi
if ! have_cmd fuser && [ ! -x /usr/sbin/fuser ]; then
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

if [ -d "$ROOT/venv" ] && [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "Removing unusable venv (often a Windows venv copied onto Linux)..."
  rm -rf "$ROOT/venv"
fi

echo "Creating virtualenv at $ROOT/venv"
run_as_real_user "cd '$ROOT' && '$PYBIN' -m venv '$ROOT/venv'"
run_as_real_user "cd '$ROOT' && source '$ROOT/venv/bin/activate' && python -m pip install --upgrade pip && python -m pip install -r '$ROOT/requirements.txt' && python -c 'import paho.mqtt.client as mqtt; print(\"paho-mqtt import ok\")'"

chmod +x "$ROOT/run_linux.sh" "$ROOT/install_centos8.sh"

if have_cmd firewall-cmd && $SUDO firewall-cmd --state >/dev/null 2>&1; then
  echo "Opening TCP ${PORT} in firewalld..."
  $SUDO firewall-cmd --permanent --add-port="${PORT}/tcp"
  $SUDO firewall-cmd --reload
  echo "firewalld: port ${PORT}/tcp allowed"
else
  echo "firewalld not running; if you cannot open the UI remotely, allow TCP ${PORT} yourself."
fi

if [ "${INSTALL_SERVICE:-0}" = "1" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "INSTALL_SERVICE=1 requires root. Re-run: sudo INSTALL_SERVICE=1 $0" >&2
    exit 1
  fi
  UNIT=/etc/systemd/system/e9-battery-monitoring.service
  cat > "$UNIT" <<EOF
[Unit]
Description=E9 Battery Monitoring dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REAL_USER}
WorkingDirectory=${ROOT}
Environment=E9_WEB_HOST=0.0.0.0
Environment=E9_WEB_PORT=${PORT}
ExecStart=${ROOT}/run_linux.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now e9-battery-monitoring.service
  echo "systemd: e9-battery-monitoring.service enabled and started"
  echo "  sudo systemctl status e9-battery-monitoring"
else
  echo
  echo "Install finished. Start the dashboard with:"
  echo "  $ROOT/run_linux.sh"
  echo "Optional boot service:"
  echo "  sudo INSTALL_SERVICE=1 $0"
fi
