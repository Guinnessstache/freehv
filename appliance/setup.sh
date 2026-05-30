#!/usr/bin/env bash
#
# FreeHV appliance provisioner.
#
# Turns a fresh Debian/Ubuntu system into a FreeHV hypervisor appliance:
# installs KVM + libvirt + the management daemon, deploys it to /opt/freehv,
# and enables the systemd service so the console comes up on boot at :5050.
#
# Two ways it runs:
#   sudo ./setup.sh              normal: install + enable + start now
#   ./setup.sh --in-target       inside the Debian installer chroot (no running
#                                systemd): install + enable only, no start
#
# Idempotent: safe to re-run.

set -euo pipefail

IN_TARGET=0
[[ "${1:-}" == "--in-target" ]] && IN_TARGET=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER_SRC="$(cd "$SCRIPT_DIR/../freehv-manager" && pwd)"
DEST=/opt/freehv
CONFIG_DIR=/var/lib/freehv

log(){ printf '\033[1;33m[freehv]\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2; exit 1
fi
if [[ ! -d "$MANAGER_SRC" ]]; then
  echo "Could not find freehv-manager next to this script ($MANAGER_SRC)." >&2; exit 1
fi

# --- 1. packages ----------------------------------------------------------
log "Installing virtualization stack and dependencies…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y

# Required core. qemu-system-x86 is the real package on both Debian & Ubuntu.
REQUIRED=(
  qemu-system-x86 qemu-utils
  libvirt-daemon-system libvirt-clients
  python3 python3-libvirt python3-flask python3-pip
  bridge-utils dnsmasq-base
)
apt-get install -y "${REQUIRED[@]}"

# Best-effort extras (don't fail the whole run if a name is unavailable).
apt-get install -y qemu-kvm ovmf 2>/dev/null || \
  log "note: qemu-kvm/ovmf not installed (often virtual/renamed — harmless)."

# flask-sock is frequently not packaged; install via pip (PEP 668 override).
if ! python3 -c 'import flask_sock' 2>/dev/null; then
  log "Installing flask-sock via pip…"
  pip3 install --break-system-packages flask-sock 2>/dev/null || \
    pip3 install flask-sock
fi

# --- 2. deploy the daemon -------------------------------------------------
log "Deploying management daemon to $DEST…"
mkdir -p "$DEST"
# When run from the installer, the repo is already at $DEST — don't copy onto
# ourselves. Otherwise (re)place a clean copy of the manager.
if [[ "$MANAGER_SRC" != "$DEST/freehv-manager" ]]; then
  rm -rf "$DEST/freehv-manager"
  cp -a "$MANAGER_SRC" "$DEST/freehv-manager"
fi
mkdir -p "$CONFIG_DIR/disks" "$CONFIG_DIR/isos"
chmod 750 "$CONFIG_DIR"

# --- 3. systemd service ---------------------------------------------------
log "Installing systemd service…"
install -m 0644 "$DEST/freehv-manager/freehv-manager.service" \
  /etc/systemd/system/freehv-manager.service

enable_now(){ systemctl "$@"; }
if [[ $IN_TARGET -eq 1 ]]; then
  # In the installer chroot there's no running systemd: enable via symlink only.
  log "(in-target) enabling services for first boot…"
  systemctl enable libvirtd.service          2>/dev/null || \
    ln -sf /lib/systemd/system/libvirtd.service \
      /etc/systemd/system/multi-user.target.wants/libvirtd.service 2>/dev/null || true
  systemctl enable freehv-manager.service     2>/dev/null || \
    ln -sf /etc/systemd/system/freehv-manager.service \
      /etc/systemd/system/multi-user.target.wants/freehv-manager.service 2>/dev/null || true
else
  systemctl daemon-reload
  log "Enabling and starting libvirt + default network…"
  systemctl enable --now libvirtd.service || true
  # Make sure the default NAT network exists and autostarts.
  virsh net-info default >/dev/null 2>&1 || \
    virsh net-define /usr/share/libvirt/networks/default.xml 2>/dev/null || true
  virsh net-autostart default 2>/dev/null || true
  virsh net-start default 2>/dev/null || true
  log "Enabling and starting FreeHV…"
  systemctl enable --now freehv-manager.service
fi

# --- 4. done --------------------------------------------------------------
cat <<EOF

  FreeHV appliance provisioning complete.

  Console:  http://<this-host>:5050
  The initial admin password was generated on first start and written to the
  journal. Retrieve it with:

      journalctl -u freehv-manager | grep 'Initial admin password'

  Then change it from the gear menu in the web console.

EOF
[[ $IN_TARGET -eq 1 ]] && echo "  (in-target install: services will start on first boot.)"
exit 0
